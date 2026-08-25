#!/usr/bin/env python3
import subprocess
import psutil
import json
import os
import hashlib
import secrets
from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for, Response
import time
from datetime import datetime, timedelta as _timedelta
from functools import wraps

# --- DMR ID -> callsign lookup ---
_dmr_ids = {}
_dmr_ids_mtime = 0
def _load_dmr_ids():
    global _dmr_ids, _dmr_ids_mtime
    path = "/opt/MMDVMHost/DMRIds.dat"
    try:
        m = os.path.getmtime(path)
        if m == _dmr_ids_mtime and _dmr_ids:
            return
        d = {}
        with open(path, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split()
                if len(parts) >= 2 and parts[0].isdigit():
                    d[parts[0]] = parts[1]
        _dmr_ids = d
        _dmr_ids_mtime = m
    except Exception:
        pass


# MCC (перші 3 цифри DMR ID) -> ISO-код країни (ITU E.212)
_MCC_RAW = ('202GR 204NL 206BE 208FR 212MC 213AD 214ES 215ES 216HU 218BA 219HR 220RS 221XK 222IT 223IT 225IT 226RO 228CH 230CZ 231SK 232AT 234GB 235GB 238DK 240SE 242NO 244FI 246LT 247LV 248EE 250RU 251RU 255UA 257BY 259MD 260PL 261PL 262DE 263DE 264DE 265DE 266GI 268PT 269PT 270LU 272IE 274IS 276AL 278MT 280CY 282GE 283AM 284BG 286TR 288FO 290GL 292SM 293SI 294MK 295LI 297ME 302CA 303CA 308PM 310US 311US 312US 313US 314US 315US 316US 317US 318US 319US 320US 321US 322US 323US 324US 325US 326US 327US 328US 329US 330PR 332VI 334MX 335MX 338JM 340GP 342BB 344AG 346KY 348VG 350BM 352GD 354MS 356KN 358LC 360VC 362CW 363AW 364BS 365AI 366DM 368CU 370DO 372HT 374TT 376TC 400AZ 401KZ 402BT 404IN 405IN 406IN 410PK 412AF 413LK 414MM 415LB 416JO 417SY 418IQ 419KW 420SA 421YE 422OM 423PS 424AE 425IL 426BH 427QA 428MN 429NP 430AE 431AE 432IR 434UZ 436TJ 437KG 438TM 440JP 441JP 450KR 452VN 454HK 455MO 456KH 457LA 460CN 461CN 466TW 467KP 470BD 472MV 502MY 505AU 510ID 514TL 515PH 520TH 525SG 528BN 530NZ 534MP 535GU 536NR 537PG 539TO 540SB 541VU 542FJ 543WF 544AS 545KI 546NC 547PF 548CK 549WS 550FM 551MH 552PW 553TV 554TK 555NU 602EG 603DZ 604MA 605TN 606LY 607GM 608SN 609MR 610ML 611GN 612CI 613BF 614NE 615TG 616BJ 617MU 618LR 619SL 620GH 621NG 622TD 623CF 624CM 625CV 626ST 627GQ 628GA 629CG 630CD 631AO 632GW 633SC 634SD 635RW 636ET 637SO 638DJ 639KE 640TZ 641UG 642BI 643MZ 645ZM 646MG 647RE 648ZW 649NA 650MW 651LS 652BW 653SZ 654KM 655ZA 657ER 658SH 659SS 702BZ 704GT 706SV 708HN 710NI 712CR 714PA 716PE 722AR 724BR 730CL 732CO 734VE 736BO 738GY 740EC 742GF 744PY 746SR 748UY 750FK')
_MCC = {x[:3]: x[3:] for x in _MCC_RAW.split()}

try:
    import sys as _sys
    if "/opt/dashboard" not in _sys.path:
        _sys.path.insert(0, "/opt/dashboard")
    from itu_flags import call_flag as _call_flag
except Exception:
    def _call_flag(cs): return ""

def _dmr_flag(raw):
    """Емодзі-прапорець країни за перші 3 цифри DMR ID."""
    try:
        raw = str(raw).strip()
        if not raw.isdigit() or len(raw) < 6:
            return ""
        cc = _MCC.get(raw[:3], "")
        if not cc:
            return ""
        return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)
    except Exception:
        return ""

def dmr_id_to_callsign(cs):
    # Конвертує числовий DMR ID у позивний, якщо є в базі
    if cs and cs.isdigit():
        _load_dmr_ids()
        return _dmr_ids.get(cs, cs)
    return cs

app = Flask(__name__)

# Кеш системних метрик
_system_cache = {}
_activity_cache = []
_today_count = 0
_today_count_ts = 0
_hourly_cache = [0]*24
# Історія метрик за добу (288 точок по 5хв): кожна точка {t, cpu, mem, temp}
_METRIC_HISTORY_FILE = "/home/pi/metric_history.json"
_metric_history_dump_ts = 0
def _load_metric_history():
    try:
        if os.path.exists(_METRIC_HISTORY_FILE):
            with open(_METRIC_HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data[-288:]
    except Exception:
        pass
    return []
_metric_history = _load_metric_history()
_metric_history_ts = 0
_top_cache = []
_network_status_cache = {
    "dmr_master": "--", "ysf_linked": "--",
    "nxdn_linked": "--", "dstar_linked": "--",
    "dstar_irc": "--", "callsign": "--",
    "dmr_id": "--", "dmr_enabled": "0",
    "ysf_enabled": "0", "nxdn_enabled": "0", "dstar_enabled": "0",
    "trx_status": "Listening", "trx_mode": ""
}


import configparser as _cph_hdr
_cp_hdr = _cph_hdr.RawConfigParser()
_cp_hdr.read("/opt/MMDVMHost/MMDVM-Host.ini")
_CALLSIGN = _cp_hdr.get("General", "Callsign", fallback="N0CALL")
_FREQ_HZ = _cp_hdr.get("Info", "RXFrequency", fallback="438100000")
_FREQ_MHZ = f"{int(_FREQ_HZ)/1000000:.3f}"

def _init_network_status_enabled():
    global _network_status_cache
    import configparser as _cp3
    try:
        cp = _cp3.RawConfigParser()
        cp.read("/opt/MMDVMHost/MMDVM-Host.ini")
        _network_status_cache["callsign"] = cp.get("General", "Callsign", fallback="--")
        _network_status_cache["dmr_id"] = cp.get("General", "Id", fallback="--")
        _network_status_cache["dmr_enabled"] = cp.get("DMR", "Enable", fallback="0")
        _network_status_cache["ysf_enabled"] = cp.get("System Fusion Network", "Enable", fallback="0")
        _network_status_cache["nxdn_enabled"] = cp.get("NXDN Network", "Enable", fallback="0")
        _network_status_cache["dstar_enabled"] = cp.get("D-Star Network", "Enable", fallback="0")
        # DMR master - read active networks from DMRGateway config
        if _network_status_cache["dmr_enabled"] == "1":
            dmr_names = []
            try:
                gwcp = _cp3.RawConfigParser(strict=False)
                gwcp.read("/opt/DMRGateway/DMRGateway.ini")
                for sec in gwcp.sections():
                    if sec.startswith("DMR Network"):
                        if gwcp.get(sec, "Enabled", fallback="0") == "1":
                            nm = gwcp.get(sec, "Name", fallback="").strip()
                            if nm:
                                dmr_names.append(nm)
            except Exception:
                pass
            if dmr_names:
                _network_status_cache["dmr_master"] = ", ".join(dmr_names)
            else:
                gw = cp.get("DMR Network", "GatewayAddress", fallback="")
                _network_status_cache["dmr_master"] = "HBLink IONOS" if "217.154.145.182" in gw else gw
        # D-Star IRC
        try:
            with open("/etc/ircddbgateway") as f:
                for line in f:
                    if "ircddbHostname=" in line:
                        _network_status_cache["dstar_irc"] = line.strip().split("=",1)[1][:16]
        except: pass
    except: pass

# Читаємо позивний і частоту з конфігу
import configparser as _cph_hdr
_cp_hdr = _cph_hdr.RawConfigParser()
_cp_hdr.read("/opt/MMDVMHost/MMDVM-Host.ini")
_CALLSIGN = _cp_hdr.get("General", "Callsign", fallback="N0CALL")
_FREQ_HZ = _cp_hdr.get("Info", "RXFrequency", fallback="438100000")
_FREQ_MHZ = f"{int(_FREQ_HZ)/1000000:.3f}"

_init_network_status_enabled()
_last_temp_update = 0
_last_activity_update = 0

# Real-time tail -f моніторинг логу
import threading as _threading

def _tail_activity():
    import glob, datetime as dt_mod, time as _time
    global _activity_cache
    log_dir = "/var/log/mmdvm"
    prefix = "MMDVM"
    
    def get_log():
        today = datetime.now().strftime("%Y-%m-%d")
        f = f"{log_dir}/{prefix}-{today}.log"
        if os.path.exists(f): return f
        files = sorted(glob.glob(f"{log_dir}/{prefix}-*.log"))
        return files[-1] if files else None

    def parse_line(line, active_tx):
        if not line.strip(): return None
        if any(x in line for x in ["XXXNOMATCH"]): return None
        try:
            parts = line.split(" ", 4)
            if len(parts) < 5: return None
            mode_raw = parts[3].rstrip(",")
            if mode_raw == "DMR" and "Slot" in parts[4]:
                slot = parts[4].split("Slot")[1].split(",")[0].strip()
                mode = "DMR Slot " + slot
            else:
                mode = mode_raw
            if mode not in ("DMR","YSF","D-Star","NXDN","P25") and not mode.startswith("DMR Slot"):
                return None
            ts_utc = parts[1] + " " + parts[2][:8]
            dt_utc = datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
            dt_local = dt_utc + dt_mod.timedelta(hours=3)
            ts = dt_local.strftime("%H:%M:%S")
            ts_epoch = (dt_utc - datetime(1970,1,1)).total_seconds()
            src = "RF" if ("received RF" in line or "RF header from" in line or "RF late entry" in line) else ("LNet" if "Begin TX" in line else "Net")
            callsign = target = ""
            flag = ""
            if "from" in line and " to " in line:
                fi = line.index(" from ") + 6
                ti = line.index(" to ", fi)
                _raw = line[fi:ti].strip()
                flag = _dmr_flag(_raw)
                callsign = dmr_id_to_callsign(_raw)
                if not flag:
                    flag = _call_flag(callsign)
                target = line[ti+4:].strip()
                if "," in target: target = target.split(",")[0].strip()
            if not callsign: return None
            key = mode + callsign
            if "end of transmission" in line or "end of voice transmission" in line:
                active_tx[key] = False
                # Парсимо dur/loss/ber
                dur = loss = ber = "---"
                try:
                    tail = line.split(", ", 2)[-1] if ", " in line else ""
                    for pt in [x.strip() for x in tail.split(",")]:
                        if "second" in pt: dur = pt.split()[0]+"s"
                        elif "packet loss" in pt: loss = pt.split()[0]
                        elif pt.startswith("BER:"): ber = pt.split()[-1]
                except: pass
                # Повертаємо спеціальний маркер для оновлення останнього запису
                return {"_update_dur": True, "mode": mode, "callsign": callsign, "dur": dur, "loss": loss, "ber": ber}
            elif any(x in line for x in ["received network","received RF","Begin TX","RF header from","received RF late entry","received RF header"]):
                active_tx[key] = True
                return {"time":ts,"hour":dt_local.hour,"epoch":ts_epoch,"mode":mode,"flag":flag,"callsign":callsign,"target":target,"src":src,"active":True,"dur":"---","loss":"---","ber":"---"}
        except: pass
        return None

    current_log = None
    file_obj = None
    active_tx = {}
    entries = []

    while True:
        try:
            log = get_log()
            if log != current_log:
                if file_obj: file_obj.close()
                current_log = log
                entries.clear()
                active_tx.clear()
                if log:
                    # Читаємо існуючі рядки для ініціалізації active_tx і entries
                    with open(log, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            e = parse_line(line, active_tx)
                            if e:
                                if e.get('_update_dur'):
                                    # Оновлюємо dur/loss/ber останнього запису з тим же mode+callsign
                                    for en in entries:
                                        if en['mode']==e['mode'] and en['callsign']==e['callsign']:
                                            en['dur']=e['dur']; en['loss']=e['loss']; en['ber']=e['ber']
                                            break
                                else:
                                    entries.insert(0, e)
                                    if len(entries) > 10000: entries.pop()
                    # Оновлюємо active прапорці
                    for e in entries:
                        e['active'] = active_tx.get(e['mode']+e['callsign'], False)
                    _activity_cache = list(entries)
                    # Відкриваємо для tail і читаємо до реального кінця
                    file_obj = open(log, encoding="utf-8", errors="ignore")
                    while file_obj.readline(): pass

                    # Тепер file_obj на справжньому кінці

            if file_obj:
                line = file_obj.readline()
                if line:
                    e = parse_line(line, active_tx)
                    if e:
                        if e.get('_update_dur'):
                            for en in entries:
                                if en['mode']==e['mode'] and en['callsign']==e['callsign']:
                                    en['dur']=e['dur']; en['loss']=e['loss']; en['ber']=e['ber']
                                    break
                        else:
                            entries.insert(0, e)
                            if len(entries) > 10000: entries.pop()
                    # TX тільки для першого (найновішого) активного рядка
                    seen_active = set()
                    for entry in entries:
                        key = entry['mode']+entry['callsign']
                        if active_tx.get(key, False) and key not in seen_active and (time.time() - entry.get('epoch', 0)) < 120:
                            entry['active'] = True
                            seen_active.add(key)
                        else:
                            entry['active'] = False
                    _activity_cache = list(entries)
                else:
                    # Навіть без нових рядків — оновлюємо active стан
                    seen_active = set()
                    for entry in entries:
                        key = entry['mode']+entry['callsign']
                        if active_tx.get(key, False) and key not in seen_active and (time.time() - entry.get('epoch', 0)) < 120:
                            entry['active'] = True
                            seen_active.add(key)
                        else:
                            entry['active'] = False
                    _activity_cache = list(entries)
                    _time.sleep(0.2)
            else:
                _time.sleep(1)
        except Exception as ex:
            _time.sleep(1)

_threading.Thread(target=_tail_activity, daemon=True).start()

def _mqtt_network_monitor():
    import paho.mqtt.client as mqtt_client
    import json as _json
    global _network_status_cache

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("ysf-gateway/json")
        client.subscribe("nxdn-gateway/json")
        client.subscribe("mmdvm/json")

    def on_message(client, userdata, msg):
        global _network_status_cache
        try:
            topic = msg.topic
            data = _json.loads(msg.payload.decode())
            if topic == "ysf-gateway/json" and "link" in data:
                lnk = data["link"]
                action = lnk.get("action","")
                if action == "unlinked":
                    _network_status_cache["ysf_linked"] = "Не підключено"
                elif "reflector" in lnk and lnk["reflector"].strip():
                    val = lnk["reflector"].strip()
                    _network_status_cache["ysf_linked"] = val
                    # Зберігаємо в файл для PHP дашборду
                    try:
                        with open("/tmp/ysf_linked.txt", "w") as _f:
                            _f.write(val)
                    except: pass
            elif topic == "nxdn-gateway/json" and "link" in data:
                lnk = data["link"]
                action = lnk.get("action","")
                if action == "unlinked":
                    _network_status_cache["nxdn_linked"] = "Не підключено"
                elif "talkgroup" in lnk:
                    tg = str(lnk["talkgroup"])
                    if tg != "9999":
                        _network_status_cache["nxdn_linked"] = "TG " + tg
            elif topic == "mmdvm/json" and "MMDVM" in data:
                mode = data["MMDVM"].get("mode","")
                if mode and mode != "idle":
                    _network_status_cache["trx_status"] = "TX " + mode
                    _network_status_cache["trx_mode"] = mode
                elif mode == "idle":
                    _network_status_cache["trx_status"] = "Listening"
                    _network_status_cache["trx_mode"] = ""
            elif topic == "mmdvm/json":
                for m in ["YSF","DMR","D-Star","NXDN","P25"]:
                    if m in data:
                        action = data[m].get("action","")
                        source = data[m].get("source","")
                        if action in ("start","late_entry"):
                            _network_status_cache["trx_status"] = "TX " + m
                            _network_status_cache["trx_mode"] = m
                        elif action == "end":
                            mode = _network_status_cache.get("trx_mode","")
                            _network_status_cache["trx_status"] = "Listening " + mode if mode else "Listening"
                        break
        except: pass

    while True:
        try:
            client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_message = on_message
            client.connect("127.0.0.1", 1883, 60)
            client.loop_forever()
        except Exception:
            import time as _t; _t.sleep(5)

_threading.Thread(target=_mqtt_network_monitor, daemon=True).start()

# Ініціалізуємо кеш при старті
def _init_network_cache():
    import glob as _gl2
    global _network_status_cache
    import time as _t; _t.sleep(2)  # Чекаємо поки MQTT підключиться
    try:
        # YSF - з journald
        out = subprocess.run(
            ["journalctl", "-u", "ysfgateway", "-n", "500", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=3
        ).stdout.strip().split("\n")
        linked = [l for l in out if "Linked to" in l and "MMDVM" not in l]
        if linked:
            val = linked[-1].split("Linked to")[-1].strip().strip('"').strip()
            if val: _network_status_cache["ysf_linked"] = val.strip()
    except: pass
    try:
        # NXDN - з логу
        import glob as _g3
        logs = sorted(_g3.glob("/var/log/mmdvm/MMDVM-*.log"))
        for log in reversed(logs):
            out = subprocess.run(["grep", "-a", "NXDN, received network", log],
                capture_output=True, text=True, timeout=2).stdout.strip().split("\n")
            lines = [l for l in out if "to TG" in l and "9999" not in l]
            if lines:
                tg = lines[-1].split("to TG")[-1].split(",")[0].strip()
                if tg.isdigit():
                    _network_status_cache["nxdn_linked"] = "TG " + tg
                break
    except: pass

_threading.Thread(target=_init_network_cache, daemon=True).start()

def _tail_dstar_links():
    import time as _t2, re as _re2, os as _os2, datetime as _dt3
    global _network_status_cache

    def get_log():
        # ircddbgateway використовує UTC для імені файлу
        today = _dt3.datetime.now(_dt3.timezone.utc).strftime("%Y-%m-%d")
        log = f"/var/log/ircddbgateway/ircDDBGateway-{today}.log"
        if _os2.path.exists(log): return log
        # Якщо немає - беремо останній наявний
        files = sorted([f for f in _os2.listdir("/var/log/ircddbgateway/") if f.startswith("ircDDBGateway-") and f.endswith(".log") and _os2.path.getsize(f"/var/log/ircddbgateway/{f}") > 0])
        return f"/var/log/ircddbgateway/{files[-1]}" if files else None

    def parse_line(line):
        if "DCS link to" in line and "established" in line:
            m = _re2.search("DCS link to ([^ ]+ [^ ]+) established", line)
            if m:
                ref = m.group(1)
                if ref.startswith("DCS"):
                    ref = "XLX" + ref[3:]
                return ref
        elif "Unlink command" in line:
            return "Не підключено"
        return None

    # Стан лінка шукаємо по файлах від найновішого до старіших:
    # подія лінкування могла статись у попередні доби і в сьогоднішній
    # лог не потрапити (лінк тримається тижнями без переліну).
    try:
        _d = "/var/log/ircddbgateway/"
        _files = sorted([f for f in _os2.listdir(_d)
                         if f.startswith("ircDDBGateway-") and f.endswith(".log")
                         and _os2.path.getsize(_d + f) > 0], reverse=True)
        for _fn in _files[:7]:
            with open(_d + _fn, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            _found = None
            for line in reversed(lines):
                val = parse_line(line.strip())
                if val:
                    _found = val
                    break
            if _found:
                _network_status_cache["dstar_linked"] = _found
                break
    except Exception:
        pass

    current_log = log
    last_pos = _os2.path.getsize(log) if log else 0

    while True:
        try:
            _t2.sleep(1)
            log = get_log()
            if log != current_log:
                current_log = log
                last_pos = 0
            if not log: continue
            cur_size = _os2.path.getsize(log)
            if cur_size > last_pos:
                with open(log, encoding="utf-8", errors="ignore") as f:
                    f.seek(last_pos)
                    new_lines = f.readlines()
                    last_pos = f.tell()
                for line in new_lines:
                    line_s = line.strip()
                    val = parse_line(line_s)
                    if val:
                        _network_status_cache["dstar_linked"] = val
        except Exception as _ex:
            _t2.sleep(2)

_threading.Thread(target=_tail_dstar_links, daemon=True).start()


_scan_cache = []
_scan_cache_ts = 0
_SCAN_TTL = 5

def _scan_log_line(line):
    """Розбирає один рядок логу MMDVM.
    Повертає (тип, дані): 'head' - початок передачі, 'end' - її закінчення."""
    try:
        if not line.strip():
            return None, None
        parts = line.split(" ", 4)
        if len(parts) < 5:
            return None, None
        mode_raw = parts[3].rstrip(",")
        if mode_raw == "DMR" and "Slot" in parts[4]:
            slot = parts[4].split("Slot")[1].split(",")[0].strip()
            mode = "DMR Slot " + slot
        else:
            mode = mode_raw
        if mode not in ("DMR", "YSF", "D-Star", "NXDN", "P25") and not mode.startswith("DMR Slot"):
            return None, None
        if "from" not in line or " to " not in line:
            return None, None
        fi = line.index(" from ") + 6
        ti = line.index(" to ", fi)
        raw = line[fi:ti].strip()
        callsign = dmr_id_to_callsign(raw)
        if not callsign:
            return None, None
        flag = _dmr_flag(raw) or _call_flag(callsign)

        if "end of transmission" in line or "end of voice transmission" in line:
            dur = loss = ber = "---"
            tail = line.split(", ", 2)[-1] if ", " in line else ""
            for pt in [x.strip() for x in tail.split(",")]:
                if "second" in pt:
                    dur = pt.split()[0] + "s"
                elif "packet loss" in pt:
                    loss = pt.split()[0]
                elif pt.startswith("BER:"):
                    ber = pt.split()[-1]
            return "end", {"mode": mode, "callsign": callsign,
                           "dur": dur, "loss": loss, "ber": ber}

        if not any(x in line for x in ["received network", "received RF", "Begin TX",
                                       "RF header from", "received RF late entry",
                                       "received RF header"]):
            return None, None

        dt_utc = datetime.strptime(parts[1] + " " + parts[2][:8], "%Y-%m-%d %H:%M:%S")
        dt_local = dt_utc + _timedelta(hours=3)
        target = line[ti + 4:].strip()
        if "," in target:
            target = target.split(",")[0].strip()
        src = "RF" if ("received RF" in line or "RF header from" in line
                       or "RF late entry" in line) else ("LNet" if "Begin TX" in line else "Net")
        return "head", {"time": dt_local.strftime("%H:%M:%S"),
                        "hour": dt_local.hour,
                        "epoch": (dt_utc - datetime(1970, 1, 1)).total_seconds(),
                        "mode": mode, "flag": flag, "callsign": callsign,
                        "target": target, "src": src, "active": False,
                        "dur": "---", "loss": "---", "ber": "---"}
    except Exception:
        pass
    return None, None

def _scan_today_log(force=False):
    """Єдиний прохід по добовому логу. Повертає передачі, найновіші першими.
    Джерело істини для лічильника, гістограми, топу і стрічки активності."""
    global _scan_cache, _scan_cache_ts
    if not force and _scan_cache_ts and (time.time() - _scan_cache_ts) < _SCAN_TTL:
        return _scan_cache
    log_file = "/var/log/mmdvm/MMDVM-%s.log" % datetime.now().strftime("%Y-%m-%d")
    entries = []
    if os.path.exists(log_file):
        try:
            with open(log_file, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    kind, data = _scan_log_line(line)
                    if kind == "head":
                        entries.append(data)
                    elif kind == "end":
                        for en in reversed(entries):
                            if en["mode"] == data["mode"] and en["callsign"] == data["callsign"]:
                                en["dur"] = data["dur"]
                                en["loss"] = data["loss"]
                                en["ber"] = data["ber"]
                                break
        except Exception:
            pass
    entries.reverse()
    _scan_cache = entries
    _scan_cache_ts = time.time()
    return entries

def _count_today_qso():
    """Кількість передач за добу (за заголовками, а не закінченнями)."""
    return len(_activity_cache) or len(_scan_today_log())

def _hourly_activity():
    """{режим: [24 числа]} - передачі по годинах (місцевий час)."""
    modes = ["all", "YSF", "NXDN", "DMR", "D-Star"]
    result = {m: [0] * 24 for m in modes}
    for e in (_activity_cache or _scan_today_log()):
        h = e.get("hour")
        if h is None or not (0 <= h < 24):
            continue
        m = e["mode"]
        mode = "DMR" if m.startswith("DMR") else (m if m in modes else None)
        result["all"][h] += 1
        if mode:
            result[mode][h] += 1
    return result

def _top_callsigns():
    """Топ-10 позивних за сьогодні з розбивкою за режимом."""
    modes = ["all", "YSF", "NXDN", "DMR", "D-Star"]
    counts = {m: {} for m in modes}
    for e in (_activity_cache or _scan_today_log()):
        cs = e.get("callsign") or ""
        cs_pure = cs.split("/")[0].split(" ")[0].strip() or cs
        if not cs_pure:
            continue
        m = e["mode"]
        mode = "DMR" if m.startswith("DMR") else (m if m in modes else None)
        counts["all"][cs_pure] = counts["all"].get(cs_pure, 0) + 1
        if mode:
            counts[mode][cs_pure] = counts[mode].get(cs_pure, 0) + 1
    result = {}
    for m in modes:
        top = sorted(counts[m].items(), key=lambda kv: kv[1], reverse=True)[:10]
        result[m] = [{"callsign": c, "count": n} for c, n in top]
    return result

def _parse_activity():
    import glob, datetime as dt_mod
    log_dir = "/var/log/mmdvm"
    prefix = "MMDVM"
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = f"{log_dir}/{prefix}-{today}.log"
    if not os.path.exists(log_file):
        files = sorted(glob.glob(f"{log_dir}/{prefix}-*.log"))
        log_file = files[-1] if files else None
    if not log_file: return []
    entries = []
    active_tx = {}  # key=mode+callsign, value=True якщо TX активний
    try:
        result = subprocess.run(
            ["grep", "-E", "received network|received RF|Begin TX|end of", log_file],
            capture_output=True, text=True, timeout=3
        )
        all_lines = result.stdout.strip().split("\n")
        # Читаємо всі рядки щоб відстежити стан TX
        for line in all_lines:
            if not line.strip(): continue
            try:
                parts = line.split(" ", 4)
                if len(parts) < 5: continue
                mode_raw = parts[3].rstrip(",")
                if mode_raw == "DMR" and "Slot" in parts[4]:
                    slot = parts[4].split("Slot")[1].split(",")[0].strip()
                    mode = "DMR Slot " + slot
                else:
                    mode = mode_raw
                if "from" in line and " to " in line:
                    from_idx = line.index(" from ") + 6
                    to_idx = line.index(" to ", from_idx)
                    callsign = line[from_idx:to_idx].strip()
                    key = mode + callsign
                    if "end of transmission" in line or "end of voice transmission" in line:
                        active_tx[key] = False
                    else:
                        active_tx[key] = True
            except: continue
        # Тепер парсимо для відображення
        for line in reversed(all_lines):
            if not line.strip(): continue
            if any(x in line for x in ["end of", "XXXNOMATCH", "RF header from", "late entry"]): continue
            try:
                parts = line.split(" ", 4)
                if len(parts) < 5: continue
                ts_utc = parts[1] + " " + parts[2][:8]
                dt_utc = datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
                dt_local = dt_utc + dt_mod.timedelta(hours=3)
                ts = dt_local.strftime("%H:%M:%S")
                ts_epoch = (dt_utc - datetime(1970,1,1)).total_seconds()
                mode_raw = parts[3].rstrip(",")
                if mode_raw == "DMR" and "Slot" in parts[4]:
                    slot = parts[4].split("Slot")[1].split(",")[0].strip()
                    mode = "DMR Slot " + slot
                else:
                    mode = mode_raw
                src = "RF" if ("received RF" in line or "RF header from" in line or "RF late entry" in line) else ("LNet" if "Begin TX" in line else "Net")
                callsign = ""
                target = ""
                if "from" in line and " to " in line:
                    from_idx = line.index(" from ") + 6
                    to_idx = line.index(" to ", from_idx)
                    _raw_src = line[from_idx:to_idx].strip()
                    flag = _dmr_flag(_raw_src)
                    callsign = dmr_id_to_callsign(_raw_src)
                    target = line[to_idx+4:].strip()
                    if "," in target: target = target.split(",")[0].strip()
                if not callsign: continue
                key = mode + callsign
                is_active = active_tx.get(key, active_tx.get(mode, False))
                entries.append({"time": ts, "epoch": ts_epoch, "mode": mode, "flag": flag,
                    "callsign": callsign, "target": target, "src": src, "active": is_active})
                if len(entries) >= 100: break
            except: continue
    except: pass
    return entries

def _update_cache():
    global _system_cache, _last_temp_update, _activity_cache, _last_activity_update
    while True:
        try:
            sys = get_system_info()
            now = time.time()
            if now - _last_temp_update >= 10:
                _system_cache = sys
                _last_temp_update = now
            else:
                sys['temp'] = _system_cache.get('temp', sys['temp'])
                _system_cache = sys
            # Скидаємо активні передачі старші 120 секунд
            now_t = time.time()
            for e in _activity_cache:
                if e.get('active') and e.get('epoch'):
                    if now_t - e['epoch'] > 120:
                        e['active'] = False
                        e['_force_inactive'] = True
        except:
            pass
        time.sleep(2)
def _record_metric_history():
    global _metric_history, _metric_history_ts, _metric_history_dump_ts
    while True:
        try:
            now = time.time()
            if now - _metric_history_ts > 300:
                tval = _system_cache.get('temp', '')
                tnum = float(str(tval).replace('\u00b0C','').strip()) if tval else None
                _mp = _system_cache.get('mem_percent', 0)
                if not _mp:
                    time.sleep(5)
                    continue
                _metric_history.append({
                    "t": int(now),
                    "cpu": _system_cache.get('cpu', 0),
                    "mem": _system_cache.get('mem_percent', 0),
                    "temp": tnum
                })
                _cut = int(now) - 86400
                _metric_history = [_p for _p in _metric_history if _p.get("t", 0) >= _cut]
                if len(_metric_history) > 2000:
                    _metric_history = _metric_history[-2000:]
                _metric_history_ts = now
            if now - _metric_history_dump_ts > 3600:
                try:
                    with open(_METRIC_HISTORY_FILE, "w", encoding="utf-8") as _hf:
                        json.dump(_metric_history, _hf)
                except Exception:
                    pass
                _metric_history_dump_ts = now
        except Exception:
            pass
        time.sleep(10)

import threading
threading.Thread(target=_update_cache, daemon=True).start()
threading.Thread(target=_record_metric_history, daemon=True).start()
app.secret_key = secrets.token_hex(32)

# Пароль адміна - хеш зберігається у файлі, fallback на стандартний
ADMIN_PASS_FILE = "/opt/dashboard/admin_pass.hash"
_DEFAULT_ADMIN_HASH = hashlib.sha256("passw0rd".encode()).hexdigest()  # ЗМІНІТЬ пароль після встановлення!

def get_admin_hash():
    try:
        if os.path.exists(ADMIN_PASS_FILE):
            with open(ADMIN_PASS_FILE, encoding="utf-8") as f:
                h = f.read().strip()
                if h:
                    return h
    except Exception:
        pass
    return _DEFAULT_ADMIN_HASH

def set_admin_hash(new_hash):
    with open(ADMIN_PASS_FILE, "w", encoding="utf-8") as f:
        f.write(new_hash)

SERVICES = [
    {"name": "mmdvmhost",     "label": "MMDVM Host",       "icon": "📡"},
    {"name": "dmrgateway",    "label": "DMR Gateway",      "icon": "💠"},
    {"name": "ircddbgateway", "label": "D-Star Gateway",   "icon": "⭐"},
    {"name": "ysfgateway",    "label": "YSF Gateway",      "icon": "🔶"},
    {"name": "nxdngateway",   "label": "NXDN Gateway",     "icon": "🔷"},
    {"name": "ysfreflector",  "label": "YSF Reflector",    "icon": "🟠"},
    {"name": "nxdnreflector", "label": "NXDN Reflector",   "icon": "🔵"},
    {"name": "mmdvm_bridge",  "label": "MMDVM Bridge",     "icon": "🌉"},
    {"name": "analog_bridge", "label": "Analog Bridge",    "icon": "🎵"},
    {"name": "md380-emu",     "label": "AMBE Emulator",    "icon": "🔧"},
]

CONFIGS = [
    {"id": "mmdvmhost",    "label": "MMDVM",       "path": "/opt/MMDVMHost/MMDVM-Host.ini",                   "service": "mmdvmhost"},
    {"id": "dmrgateway",   "label": "DMR GW",      "path": "/opt/DMRGateway/DMRGateway.ini",                 "service": "dmrgateway"},
    {"id": "ircddb",       "label": "ircDDB Gateway",   "path": "/etc/ircddbgateway",                              "service": "ircddbgateway"},
    {"id": "ysfgateway",   "label": "YSF GW",      "path": "/opt/YSFClients/YSFGateway/YSFGateway.ini",       "service": "ysfgateway"},
    {"id": "ysfreflector", "label": "YSF Refl.",    "path": "/opt/pYSFReflector/YSFReflector.ini",             "service": "ysfreflector"},
    {"id": "nxdngateway",  "label": "NXDN GW",     "path": "/opt/NXDNClients/NXDNGateway/NXDNGateway.ini",    "service": "nxdngateway"},
    {"id": "nxdnreflector","label": "NXDN Refl.",   "path": "/opt/DVReflectors/NXDNReflector/NXDNReflector.ini","service": "nxdnreflector"},
    {"id": "mmdvmbridge",  "label": "Bridge",     "path": "/opt/MMDVM_Bridge/MMDVM_Bridge.ini",              "service": "mmdvm_bridge"},
    {"id": "dvswitch",     "label": "DVSwitch",         "path": "/opt/MMDVM_Bridge/DVSwitch.ini",                  "service": "mmdvm_bridge"},
    {"id": "analogbridge", "label": "Analog",    "path": "/opt/Analog_Bridge/Analog_Bridge.ini",            "service": "analog_bridge"},
]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def get_service_status(name):
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        active = result.stdout.strip() == "active"
        result2 = subprocess.run(["systemctl", "show", name, "--property=ActiveEnterTimestamp,InactiveEnterTimestamp,MainPID,MemoryCurrent"], capture_output=True, text=True, timeout=3)
        props = {}
        for line in result2.stdout.strip().split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                props[k] = v
        uptime = ""
        if props.get("ActiveEnterTimestamp") and props["ActiveEnterTimestamp"] != "n/a":
            try:
                parts = props["ActiveEnterTimestamp"].split()
                ts_str = " ".join(parts[1:3]) if len(parts) >= 3 else ""
                if not ts_str: raise ValueError("bad ts")
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                # Час старту системи
                with open("/proc/uptime") as _f:
                    boot_seconds = float(_f.read().split()[0])
                boot_ts = datetime.now() - __import__('datetime').timedelta(seconds=boot_seconds)
                # Беремо більший з двох (сервіс не міг запуститись раніше системи)
                if ts < boot_ts:
                    ts = boot_ts
                delta = datetime.now() - ts
                days = int(delta.total_seconds() // 86400)
                h = int((delta.total_seconds() % 86400) // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                if days > 0:
                    uptime = f"{days}д {h}г {m}хв"
                elif h > 0:
                    uptime = f"{h}г {m}хв"
                else:
                    uptime = f"{m}хв"
            except:
                uptime = ""
        pid = props.get("MainPID", "0")
        mem = ""
        try:
            mem_bytes = int(props.get("MemoryCurrent", "0"))
            if mem_bytes > 0:
                mem = f"{mem_bytes // 1024 // 1024}MB"
        except:
            pass
        # Час зупинки якщо сервіс неактивний
        stopped_ago = ""
        if not active and props.get("InactiveEnterTimestamp") and props["InactiveEnterTimestamp"] != "n/a":
            try:
                parts2 = props["InactiveEnterTimestamp"].split()
                ts2_str = " ".join(parts2[1:3]) if len(parts2) >= 3 else ""
                if ts2_str:
                    ts2 = datetime.strptime(ts2_str, "%Y-%m-%d %H:%M:%S")
                    delta2 = datetime.now() - ts2
                    h2 = int(delta2.total_seconds() // 3600)
                    m2 = int((delta2.total_seconds() % 3600) // 60)
                    stopped_ago = f"{h2}г {m2}хв" if h2 > 0 else f"{m2}хв"
            except: pass
        return {"active": active, "uptime": uptime, "stopped_ago": stopped_ago, "pid": pid if pid != "0" else "", "memory": mem}
    except:
        return {"active": False, "uptime": "", "pid": "", "memory": ""}

_sys_cache = {"temp": "", "uptime": "", "disk_used": 0, "disk_total": 0, "disk_percent": 0, "temp_ts": 0, "uptime_ts": 0, "disk_ts": 0}

def get_system_info():
    now = time.time()
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    # temp - кеш 30с
    if now - _sys_cache["temp_ts"] > 15:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                _sys_cache["temp"] = f"{int(f.read().strip()) / 1000:.1f}°C"
        except:
            pass
        _sys_cache["temp_ts"] = now
    temp = _sys_cache["temp"]
    # disk - кеш 60с
    if now - _sys_cache["disk_ts"] > 60:
        try:
            disk = psutil.disk_usage('/')
            _sys_cache["disk_used"] = disk.used // 1024 // 1024 // 1024
            _sys_cache["disk_total"] = disk.total // 1024 // 1024 // 1024
            _sys_cache["disk_percent"] = disk.percent
        except:
            pass
        _sys_cache["disk_ts"] = now
    # uptime - кеш 5с (/proc/uptime у RAM, читання дешеве)
    if now - _sys_cache["uptime_ts"] > 5:
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
                days = int(secs // 86400)
                h = int((secs % 86400) // 3600)
                m = int((secs % 3600) // 60)
                if days > 0:
                    _sys_cache["uptime"] = f"{days}д {h}г {m}хв"
                elif h > 0:
                    _sys_cache["uptime"] = f"{h}г {m}хв"
                else:
                    _sys_cache["uptime"] = f"{m}хв"
        except:
            pass
        _sys_cache["uptime_ts"] = now
    uptime = _sys_cache["uptime"]
    return {
        "cpu": cpu,
        "mem_used": mem.used // 1024 // 1024,
        "mem_total": mem.total // 1024 // 1024,
        "mem_percent": mem.percent,
        "disk_used": _sys_cache["disk_used"],
        "disk_total": _sys_cache["disk_total"],
        "disk_percent": _sys_cache["disk_percent"],
        "temp": temp,
        "uptime": uptime
    }

def get_log_lines(service, n=30):
    try:
        result = subprocess.run(["journalctl", "-u", service, "-n", str(n), "--no-pager", "-o", "short"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip().split('\n')[-n:]
    except:
        return []

@app.route("/")
def index():
    # Читаємо позивний і частоту при кожному запиті
    import configparser as _cph_req
    _cp_req = _cph_req.RawConfigParser()
    _cp_req.read("/opt/MMDVMHost/MMDVM-Host.ini")
    _cs_req = _cp_req.get("General", "Callsign", fallback="N0CALL")
    _rx_req = _cp_req.get("Info", "RXFrequency", fallback="438100000")
    _freq_req = f"{int(_rx_req)/1000000:.3f}"
    return render_template_string(HTML_TEMPLATE.replace("{_CALLSIGN}", _cs_req).replace("{_FREQ_MHZ}", _freq_req))

@app.route("/api/status")
def api_status():
    services = []
    for s in SERVICES:
        status = get_service_status(s["name"])
        services.append({**s, **status})
    return jsonify({"services": services, "system": get_system_info(), "time": datetime.now().strftime("%H:%M:%S"), "admin": bool(session.get('admin'))})

@app.route("/api/logs/<service>")
def api_logs(service):
    names = [s["name"] for s in SERVICES]
    if service not in names:
        return jsonify({"error": "unknown service"}), 400
    return jsonify({"lines": get_log_lines(service, 30)})

# Захист від перебору пароля: {IP: [кількість невдач, час останньої]}
_login_fails = {}
_login_attempts = []   # [{ip, time}] - невдалі спроби від останнього входу
_LOGIN_ATTEMPTS_MAX = 50
_login_lock = threading.Lock()
_MAX_FAILS = 5
_BLOCK_SEC = 3600  # 1 година

def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"

@app.route("/api/login", methods=["POST"])
def api_login():
    ip = _client_ip()
    now = time.time()

    with _login_lock:
        # Прибираємо застарілі записи, щоб словник не ріс безмежно
        for k in [k for k, v in _login_fails.items() if now - v[1] > _BLOCK_SEC]:
            _login_fails.pop(k, None)
        fails, last = _login_fails.get(ip, (0, 0))
        if fails >= _MAX_FAILS and now - last < _BLOCK_SEC:
            left = int((_BLOCK_SEC - (now - last)) / 60) + 1
            return jsonify({"ok": False,
                            "error": "Забагато спроб. Спробуйте через %d хв." % left}), 429

    data = request.get_json(silent=True) or {}
    pw = data.get("password", "")

    if hashlib.sha256(pw.encode()).hexdigest() == get_admin_hash():
        with _login_lock:
            _login_fails.pop(ip, None)
            attempts = list(_login_attempts)
        session['admin'] = True
        return jsonify({"ok": True, "failed_attempts": attempts})

    # Прогресивна затримка за номером спроби: 5, 10, 20, 30, 60 секунд
    with _login_lock:
        _prev, _ = _login_fails.get(ip, (0, 0))
    _delays = [5, 10, 20, 30, 60]
    time.sleep(_delays[min(_prev, len(_delays) - 1)])

    with _login_lock:
        f, _ = _login_fails.get(ip, (0, 0))
        _login_fails[ip] = (f + 1, now)
        left = _MAX_FAILS - (f + 1)
        _login_attempts.append({
            "ip": ip,
            "time": datetime.now().strftime("%d.%m %H:%M:%S")
        })
        if len(_login_attempts) > _LOGIN_ATTEMPTS_MAX:
            del _login_attempts[0]
    try:
        print("[auth] невдалий вхід з %s (лишилось спроб: %d)" % (ip, max(left, 0)), flush=True)
    except Exception:
        pass
    return jsonify({"ok": False, "error": "Невірний пароль"}), 401

@app.route("/api/login_attempts/clear", methods=["POST"])
@login_required
def api_login_attempts_clear():
    with _login_lock:
        _login_attempts.clear()
    return jsonify({"ok": True})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop('admin', None)
    return jsonify({"ok": True})

@app.route("/api/change_password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json()
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if hashlib.sha256(old_pw.encode()).hexdigest() != get_admin_hash():
        return jsonify({"ok": False, "error": "Невірний поточний пароль"}), 401
    if len(new_pw) < 6:
        return jsonify({"ok": False, "error": "Новий пароль має містити щонайменше 6 символів"}), 400
    try:
        set_admin_hash(hashlib.sha256(new_pw.encode()).hexdigest())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": "Помилка запису: " + str(e)[:60]}), 500

@app.route("/api/configs")
@login_required
def api_configs():
    configs = []
    for c in CONFIGS:
        exists = os.path.exists(c["path"])
        configs.append({**c, "exists": exists})
    return jsonify({"configs": configs})

@app.route("/api/config/<config_id>")
@login_required
def api_config_get(config_id):
    cfg = next((c for c in CONFIGS if c["id"] == config_id), None)
    if not cfg:
        return jsonify({"error": "not found"}), 404
    try:
        with open(cfg["path"], "r") as f:
            content = f.read()
        return jsonify({"content": content, "path": cfg["path"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config/<config_id>", methods=["POST"])
@login_required
def api_config_save(config_id):
    cfg = next((c for c in CONFIGS if c["id"] == config_id), None)
    if not cfg:
        return jsonify({"error": "not found"}), 404
    data = request.get_json()
    content = data.get("content", "")
    try:
        # Backup
        backup_path = cfg["path"] + ".bak"
        if os.path.exists(cfg["path"]):
            with open(cfg["path"], "r") as f:
                orig = f.read()
            with open(backup_path, "w") as f:
                f.write(orig)
        with open(cfg["path"], "w") as f:
            f.write(content)
        return jsonify({"ok": True, "backup": backup_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/service/<name>/restart", methods=["POST"])
@login_required
def api_restart(name):
    names = [s["name"] for s in SERVICES] + ["dashboard"]
    if name not in names:
        return jsonify({"error": "unknown service"}), 400
    try:
        result = subprocess.run(["systemctl", "restart", name], capture_output=True, text=True, timeout=15)
        return jsonify({"ok": result.returncode == 0, "output": result.stdout + result.stderr})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/service/<name>/stop", methods=["POST"])
@login_required
def api_stop(name):
    names = [s["name"] for s in SERVICES]
    if name not in names:
        return jsonify({"error": "unknown service"}), 400
    try:
        result = subprocess.run(["systemctl", "stop", name], capture_output=True, text=True, timeout=15)
        return jsonify({"ok": result.returncode == 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/service/<name>/start", methods=["POST"])
@login_required
def api_start(name):
    names = [s["name"] for s in SERVICES]
    if name not in names:
        return jsonify({"error": "unknown service"}), 400
    try:
        result = subprocess.run(["systemctl", "start", name], capture_output=True, text=True, timeout=15)
        return jsonify({"ok": result.returncode == 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# --- Update checker endpoints ---
UPDATE_SERVICES = ["mmdvmhost", "dmrgateway", "ysfgateway", "nxdngateway"]
UPDATE_REPOS = {
    "mmdvmhost": "/opt/MMDVMHost",
    "dmrgateway": "/opt/DMRGateway",
    "ysfgateway": "/opt/YSFClients",
    "nxdngateway": "/opt/NXDNClients",
}

@app.route("/api/updates/check")
@login_required
def api_updates_check():
    result = {}
    for name, gitdir in UPDATE_REPOS.items():
        try:
            subprocess.run(["git", "-C", gitdir, "fetch", "--quiet"],
                           capture_output=True, text=True, timeout=30)
            branch = subprocess.run(["git", "-C", gitdir, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            behind = subprocess.run(["git", "-C", gitdir, "rev-list", "--count",
                                     "HEAD..origin/%s" % branch],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            result[name] = {"behind": int(behind) if behind.isdigit() else 0, "branch": branch}
        except Exception as e:
            result[name] = {"error": str(e)}
    return jsonify(result)

@app.route("/api/updates/apply/<name>", methods=["POST"])
@login_required
def api_updates_apply(name):
    if name not in UPDATE_SERVICES:
        return jsonify({"error": "unknown service"}), 400
    try:
        result = subprocess.run(["/usr/local/bin/update-service", name],
                                capture_output=True, text=True, timeout=600)
        return jsonify({
            "ok": result.returncode == 0,
            "code": result.returncode,
            "output": result.stdout + result.stderr,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "timeout (>10min)"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_CALLSIGN} MMDVM Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&family=Noto+Color+Emoji&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0e1a; --surface: #0f1629; --surface2: #151c35; --border: #1e2d50;
  --accent: #00d4ff; --accent2: #7c3aed; --green: #00ff88; --red: #ff3860;
  --yellow: #ffdd57; --text: #e2e8f0; --muted: #ffdd57;
  --font-mono: 'JetBrains Mono', 'Noto Color Emoji', monospace; --font-main: 'Inter', -apple-system, 'Segoe UI', 'Noto Color Emoji', sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:var(--font-main); min-height:100vh; overflow-x:hidden; }
body::before { content:''; position:fixed; inset:0; background:radial-gradient(ellipse at 20% 20%,rgba(0,212,255,.05) 0%,transparent 50%),radial-gradient(ellipse at 80% 80%,rgba(124,58,237,.05) 0%,transparent 50%); pointer-events:none; z-index:0; }
.header { position:relative; z-index:1; padding:12px 24px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:linear-gradient(180deg,rgba(0,212,255,.03) 0%,transparent 100%); }
.header-left { display:flex; align-items:center; gap:16px; }
.logo { width:48px; height:48px; border:2px solid var(--accent); border-radius:12px; display:flex; align-items:center; justify-content:center; font-size:24px; box-shadow:0 0 20px rgba(0,212,255,.3); animation:pulse-border 3s ease-in-out infinite; }
@keyframes pulse-border { 0%,100%{box-shadow:0 0 20px rgba(0,212,255,.3)}50%{box-shadow:0 0 40px rgba(0,212,255,.6)} }
.title { font-size:22px; font-weight:800; letter-spacing:2px; }
.title span { color:var(--accent); }
.subtitle { font-size:14px; color:var(--muted); font-family:var(--font-mono); letter-spacing:1px; }
.header-right { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.clock { font-family:var(--font-mono); font-size:20px; color:var(--accent); letter-spacing:3px; }
.live-dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); animation:blink 1s ease-in-out infinite; }
@keyframes blink { 0%,100%{opacity:1}50%{opacity:.2} }
.btn { padding:0 20px; border-radius:10px; border:1px solid var(--border); background:var(--surface); color:var(--text); font-family:var(--font-mono); font-size:15px; cursor:pointer; transition:all .2s; font-weight:500; height:48px; display:inline-flex; flex-direction:row; align-items:center; justify-content:center; gap:6px; text-decoration:none; box-sizing:border-box; white-space:nowrap; }
.btn:hover { border-color:var(--accent); color:var(--accent); }
.btn-primary { background:rgba(0,212,255,.1); border-color:var(--accent); color:var(--accent); }
.btn-danger { background:rgba(255,56,96,.1); border-color:var(--red); color:var(--red); }
.btn-success { background:rgba(0,255,136,.1); border-color:var(--green); color:var(--green); }
.btn-sm { padding:4px 10px; font-size:13px; }
.main { position:relative; z-index:1; padding:24px 32px; }
.grid-system { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:24px; }
.stat-card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 18px; position:relative; overflow:hidden; transition:border-color .3s; }
.stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg,var(--accent),var(--accent2)); }
.stat-card.tx-active { border:2px solid #ff3860 !important; background:rgba(255,56,96,0.12) !important; box-shadow:0 0 20px rgba(255,56,96,0.7) !important; }
.stat-card.tx-active::before { background:#ff3860 !important; }
.stat-card.tx-active .stat-label { color:#ff3860 !important; }
.stat-label { font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:2px; margin-bottom:8px; }
.stat-value { font-size:28px; font-weight:800; font-family:var(--font-mono); color:var(--accent); }
.stat-sub { font-size:14px; color:var(--muted); margin-top:4px; }
.progress-bar { height:4px; background:var(--border); border-radius:2px; margin-top:12px; overflow:hidden; }
.progress-fill { height:100%; border-radius:2px; background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width 3s linear; }
.section-title { font-size:14px; color:var(--muted); text-transform:uppercase; letter-spacing:3px; margin-bottom:16px; font-family:var(--font-mono); display:flex; align-items:center; gap:8px; }
.section-title::after { content:''; flex:1; height:1px; background:var(--border); }
.services-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:6px; margin-bottom:16px; }
.service-card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:6px 8px; cursor:pointer; transition:all .3s; position:relative; overflow:hidden; display:flex; align-items:center; gap:6px; min-width:0; }
.service-card:hover { border-color:var(--accent); transform:translateY(-2px); }
.service-card.active { border-color:rgba(0,255,136,.4); }
.service-card.active::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--green); box-shadow:0 0 10px var(--green); }
.service-card.inactive { border-color:rgba(255,56,96,.3); }
.service-card.inactive::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background:var(--red); }
.service-info { flex:1; min-width:0; }
.service-icon { width:24px; height:24px; border-radius:6px; background:var(--surface2); display:flex; align-items:center; justify-content:center; font-size:13px; flex-shrink:0; }
.service-name { font-size:11px; font-weight:500; overflow:hidden; text-overflow:ellipsis; }
.service-status { font-size:10px; white-space:nowrap; }
.service-meta { font-size:10px; color:var(--muted); }
.service-info { flex:1; min-width:0; }
.service-icon { width:24px; height:24px; border-radius:6px; background:var(--surface2); display:flex; align-items:center; justify-content:center; font-size:13px; flex-shrink:0; }
.service-name { font-size:11px; font-weight:500; overflow:hidden; text-overflow:ellipsis; }
.service-status { font-size:10px; white-space:nowrap; }
.service-meta { font-size:10px; color:var(--muted); }
.service-icon { font-size:20px; margin-bottom:8px; }
.service-name { font-size:14px; font-weight:600; margin-bottom:4px; }
.service-status { display:inline-flex; align-items:center; gap:4px; font-size:12px; font-family:var(--font-mono); padding:2px 8px; border-radius:20px; }
.service-status.up { background:rgba(0,255,136,.1); color:var(--green); }
.service-status.down { background:rgba(255,56,96,.1); color:var(--red); }
.service-meta { font-size:12px; color:var(--muted); margin-top:6px; font-family:var(--font-mono); }
.service-actions { display:none; }
.logs-panel { background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-bottom:24px; }
.filter-btn { background:var(--surface); border:1px solid var(--border); color:var(--text); border-radius:8px; padding:6px 14px; font-size:13px; font-weight:600; cursor:pointer; transition:all .2s; font-family:inherit; }
.filter-btn:hover { border-color:var(--accent); }
.filter-btn.active { background:var(--accent); color:#04121c !important; border-color:var(--accent); font-weight:700; }
.activity-panel { background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-bottom:16px; }
.activity-tabs { display:flex; gap:0; border-bottom:1px solid var(--border); }
.activity-tab { padding:10px 20px; background:transparent; border:none; color:var(--muted); font-family:var(--font-mono); font-size:12px; cursor:pointer; transition:all .2s; border-bottom:2px solid transparent; }
.activity-tab:hover { color:var(--text); }
.activity-tab.active { color:var(--accent); border-bottom:2px solid var(--accent); background:rgba(0,212,255,.05); }
.activity-table-wrap { overflow-x:auto; }
.activity-table { width:100%; border-collapse:collapse; font-size:14px; font-family:var(--font-mono); }
.activity-table th { padding:10px 12px; background:var(--surface2); color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.5px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }
.activity-table td { padding:9px 12px; border-bottom:.5px solid var(--border); color:var(--text); white-space:nowrap; }
.activity-table tr:last-child td { border-bottom:none; }
.activity-table tbody tr { transition:background .15s ease, box-shadow .15s ease; }
.activity-table tbody tr:hover td { background:rgba(0,212,255,.07); }
.activity-table tbody tr:hover td:first-child { box-shadow:inset 3px 0 0 var(--accent); }
.mode-badge { display:inline-block; padding:3px 8px; border-radius:4px; font-size:12px; font-weight:600; }
.m-extra { display:none; }
.mode-dmr { background:rgba(45,204,112,.15); color:#2dcc70; }
.mode-ysf { background:rgba(255,165,0,.15); color:#ffa500; }
.mode-dstar { background:rgba(122,94,255,.15); color:#7a5eff; }
.mode-nxdn { background:rgba(74,158,255,.15); color:#4a9eff; }
.mode-p25 { background:rgba(255,87,87,.15); color:#ff5757; }
.src-lnet { display:inline-block; padding:2px 7px; border-radius:3px; font-size:12px; background:rgba(45,204,112,.2); color:#2dcc70; }
.src-net { display:inline-block; padding:2px 7px; border-radius:3px; font-size:12px; background:var(--surface2); color:var(--muted); }
.src-rf { display:inline-block; padding:2px 7px; border-radius:3px; font-size:12px; background:rgba(255,165,0,0.2); color:#ffa500; font-weight:600; }
.logs-header { padding:14px 20px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:var(--surface2); }
.logs-header-title { font-size:13px; font-weight:600; display:flex; align-items:center; gap:8px; }
.logs-tabs { display:flex; gap:4px; flex-wrap:wrap; }
.log-tab { padding:4px 10px; border-radius:6px; font-size:13px; cursor:pointer; border:1px solid var(--border); background:transparent; color:var(--muted); font-family:var(--font-mono); transition:all .2s; }
.log-tab:hover { border-color:var(--accent); color:var(--accent); }
.log-tab.active { background:rgba(0,212,255,.1); border-color:var(--accent); color:var(--accent); }
.logs-header-title { display:none !important; }
.logs-body { padding:16px 20px; height:280px; overflow-y:auto; font-family:var(--font-mono); font-size:13px; line-height:1.8; }
.logs-body::-webkit-scrollbar { width:4px; }
.logs-body::-webkit-scrollbar-track { background:var(--bg); }
.logs-body::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.log-line { color:var(--muted); white-space:pre-wrap; word-break:break-all; }
.log-line.error { color:var(--red); }
.log-line.warn { color:var(--yellow); }
.log-line.info { color:var(--text); }
.log-line.ok { color:var(--green); }
.port-badge { display:inline-block; padding:1px 6px; border-radius:4px; font-size:9px; font-family:var(--font-mono); background:rgba(0,212,255,.1); color:var(--accent); border:1px solid rgba(0,212,255,.2); margin-top:4px; }

/* Modal */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.8); z-index:100; display:none; align-items:center; justify-content:center; backdrop-filter:blur(4px); }
.modal-overlay.open { display:flex; }
.modal { background:var(--surface); border:1px solid var(--border); border-radius:16px; width:90%; max-width:900px; max-height:90vh; display:flex; flex-direction:column; overflow:hidden; }
.modal-header { padding:20px 24px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:var(--surface2); }
.modal-title { font-size:16px; font-weight:700; display:flex; align-items:center; gap:10px; }
.modal-close { background:none; border:none; color:var(--muted); font-size:20px; cursor:pointer; padding:4px; border-radius:4px; }
.modal-close:hover { color:var(--text); }
.modal-body { flex:1; overflow:hidden; display:flex; flex-direction:column; padding:20px 24px; gap:16px; }
.config-tabs { display:flex; gap:8px; flex-wrap:wrap; }
.config-tab { padding:6px 14px; border-radius:8px; font-size:14px; cursor:pointer; border:1px solid var(--border); background:transparent; color:var(--muted); font-family:var(--font-mono); transition:all .2s; }
.config-tab:hover { border-color:var(--accent); color:var(--accent); }
.config-tab.active { background:rgba(0,212,255,.1); border-color:var(--accent); color:var(--accent); }
.config-path { font-size:13px; color:var(--muted); font-family:var(--font-mono); padding:8px 12px; background:var(--bg); border-radius:6px; border:1px solid var(--border); }
.config-editor { flex:1; min-height:400px; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:16px; font-family:var(--font-mono); font-size:14px; color:var(--text); resize:none; outline:none; line-height:1.6; }
.config-editor:focus { border-color:var(--accent); }
.modal-footer { padding:16px 24px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:var(--surface2); }
.footer-info { font-size:13px; color:var(--muted); font-family:var(--font-mono); }

/* Login modal */
.login-modal { background:var(--surface); border:1px solid var(--border); border-radius:16px; width:360px; padding:32px; }
.login-title { font-size:18px; font-weight:700; margin-bottom:8px; }
.login-sub { font-size:14px; color:var(--muted); font-family:var(--font-mono); margin-bottom:24px; }
.input-field { width:100%; padding:12px 16px; background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--font-mono); font-size:14px; outline:none; transition:border-color .2s; }
.input-field:focus { border-color:var(--accent); }
.login-error { font-size:14px; color:var(--red); margin-top:8px; font-family:var(--font-mono); display:none; }

/* Toast */
.toast { position:fixed; bottom:24px; right:24px; padding:12px 20px; border-radius:10px; font-size:13px; font-family:var(--font-mono); z-index:200; opacity:0; transition:opacity .3s; pointer-events:none; }
.toast.show { opacity:1; }
.toast.success { background:rgba(0,255,136,.15); border:1px solid var(--green); color:var(--green); }
.toast.error { background:rgba(255,56,96,.15); border:1px solid var(--red); color:var(--red); }

@media (min-width: 769px) {
  .time-hdr { color:var(--yellow) !important; }
  .stat-card { display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; }
  .progress-bar { width:100%; }
  .stat-card::before { top:0; }

  .stat-label { font-size:15px; letter-spacing:1px; }
  .stat-value { font-size:32px; }
  .stat-sub { font-size:16px; }
  .section-title { font-size:16px; }
  #ns-dmr-val, #ns-ysf-val, #ns-nxdn-val, #ns-dstar-val { font-size:17px !important; }
  .activity-table th { font-size:14px; }
  .activity-table td { font-size:16px; }
  .mode-badge { font-size:13px; padding:4px 9px; }
  .src-net, .src-rf, .src-lnet { font-size:13px; padding:3px 8px; }
}
@media (max-width: 768px) {
  .col-target, .col-dur, .col-loss, .col-ber, .col-date { display:none; }
  .m-extra { display:block; }
  .header { padding:12px 14px; flex-direction:column; align-items:center; gap:10px; }
  .header-left { justify-content:center; width:100%; }
  .header-right { width:100%; justify-content:center; }
  .header-left { gap:10px; }
  .header-right { gap:8px; flex-wrap:wrap; }
  .title { font-size:18px !important; }
  .subtitle { font-size:12px !important; }
  .clock { font-size:15px; letter-spacing:1px; }
  .main { padding:14px 12px; }
  .grid-system { grid-template-columns:repeat(2,1fr); gap:10px; margin-bottom:16px; }
  .stat-card { display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; }
  .stat-card .progress-bar { width:100%; }
  .services-grid { grid-template-columns:repeat(2,1fr); gap:10px; }
  .btn { padding:6px 10px; font-size:13px; }
  .logs-body { height:calc(100vh - 120px); min-height:400px; font-size:12px; }
  .logs-header { flex-direction:column; align-items:stretch; gap:6px; }
  .logs-header-title { display:none; }
  .logs-tabs { flex-wrap:nowrap; overflow-x:auto; -webkit-overflow-scrolling:touch; padding-bottom:4px; }
  .log-tab { white-space:nowrap; flex-shrink:0; }
  .services-grid { grid-template-columns:repeat(2,1fr) !important; }
  .service-card { flex-direction:column; align-items:flex-start; padding:8px; gap:4px; }
  .service-icon { width:20px; height:20px; font-size:11px; }
  .service-name { font-size:11px; white-space:normal; }
  .service-info { width:100%; }
  .header-right { flex-wrap:wrap; gap:8px; }
  .header-right .btn { flex:1; min-width:calc(33% - 8px); text-align:center; justify-content:center; padding:12px 6px; font-size:12px; height:52px; align-items:center; display:flex; white-space:normal; line-height:1.2; overflow:hidden; }
  .clock { font-size:28px !important; text-align:center; width:100%; margin:4px 0 0 0 !important; order:99; }
  .header-left { justify-content:center; width:100%; }
  .header-left .title { font-size:22px !important; }
  .header-left .subtitle { font-size:11px !important; }
  .modal { width:96%; max-height:94vh; }
  #configs-modal .modal { height:94vh; }
  .modal-header { padding:10px 16px; border-bottom:none; }
  .modal-title { font-size:16px; }
  .modal-body { padding:6px 14px 8px; gap:6px; }
  .config-path { padding:6px 10px !important; font-size:12px; }
  .config-tabs { flex-wrap:nowrap; overflow-x:auto; max-height:none; flex-shrink:0; padding-bottom:4px; -webkit-overflow-scrolling:touch; }
  .config-tab { white-space:nowrap; padding:6px 12px; font-size:13px; flex-shrink:0; }
  .config-editor { min-height:0; flex:1; font-size:13px; padding:10px; }
  .config-path { flex-shrink:0; }
  .modal-footer { flex-wrap:wrap; gap:8px; padding:12px 14px; }
  .footer-info { width:100%; font-size:11px; order:-1; }
  .modal-footer > div { width:100%; display:flex; gap:8px; }
  .modal-footer .btn { flex:1; font-size:11px; padding:8px 6px; white-space:nowrap; height:auto; min-height:0; line-height:1.3; }
}
@media (max-width: 430px) {
  .modal-header { padding:6px 16px !important; min-height:0 !important; }
  .modal-title { font-size:15px !important; margin:0 !important; }
  .modal-body { padding-top:6px !important; }
  .grid-system { grid-template-columns:repeat(2,1fr); }
  .services-grid { grid-template-columns:1fr 1fr; }
  .header-right .btn span, .subtitle { display:none; }
  .title { font-size:16px !important; }
  #network-status-block { grid-template-columns:repeat(2,1fr) !important; }
  .col-loss { display:table-cell !important; }
  .m-extra { display:block !important; }
  /* ховаємо окрему колонку Позивний (вона тепер під Режимом) */
  .activity-table th:nth-child(3), .activity-table td:nth-child(3) { display:none !important; }
  /* ховаємо Ціль на мобільному - не влазить */
  .col-target { display:none !important; }
  .activity-table th, .activity-table td { padding:8px 6px; font-size:13px; }
}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <div class="logo">📡</div>
    <div>
      <div class="title">{_CALLSIGN} <span>MMDVM</span></div>
      <div class="subtitle">HOTSPOT CONTROL CENTER // {_FREQ_MHZ} MHz</div>
    </div>
  </div>
  <div class="header-right">
    <a class="btn" href="/live/" target="_blank"><div class="live-dot"></div>Live</a>
    <a class="btn" href="/ysf/" target="_blank">🟠 YSF</a>
    <a class="btn" href="/nxdn/" target="_blank">🔵 NXDN</a>
    <a class="btn" href="/dvswitch/" target="_blank">📊 DVS</a>
    <a class="btn" href="/mmdvm/" target="_blank" id="mmdvm-btn" style="display:none">📡 MMDVM</a>
    <button class="btn" id="bmtg-btn" style="display:none" onclick="openBmTg()">⭐ Статичні TG</button>
    <button class="btn" id="admin-btn" onclick="openAdmin()">🔐 Адмін</button>
    <button class="btn" id="configs-btn" onclick="openConfigs()" style="display:none">⚙️ Конфіги</button>
    <button class="btn" id="updates-btn" onclick="openUpdates()" style="display:none">🔄 Оновлення</button>
    <button class="btn btn-sm" id="chpass-btn" onclick="document.getElementById('chpass-modal').classList.add('open')" style="display:none">Змінити пароль</button>
    <button class="btn btn-danger btn-sm" id="logout-btn" onclick="doLogout()" style="display:none">Вийти</button>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</div>

<div class="main">
  <div class="grid-system">
    <div class="stat-card" style="cursor:pointer" onclick="openChart('cpu')">
      <div class="stat-label">Завантаження ЦП</div>
      <div class="stat-value" id="cpu">0%</div>
      <div class="progress-bar"><div class="progress-fill" id="cpu-bar" style="width:0%"></div></div>
    </div>
    <div class="stat-card" style="cursor:pointer" onclick="openChart('mem')">
      <div class="stat-label">Пам'ять</div>
      <div class="stat-value" id="mem">0%</div>
      <div class="stat-sub" id="mem-sub">0 / 0 MB</div>
      <div class="progress-bar"><div class="progress-fill" id="mem-bar" style="width:0%"></div></div>
    </div>
    <div class="stat-card" style="cursor:pointer" onclick="openChart('temp')">
      <div class="stat-label">Температура</div>
      <div class="stat-value" id="temp">--</div>
      <div class="stat-sub">Raspberry Pi 3</div>
    </div>
    <div class="stat-card" style="cursor:pointer" onclick="openDiskChart()">
      <div class="stat-label">Час роботи</div>
      <div class="stat-value" style="font-size:20px" id="uptime">--</div>
      <div class="stat-sub" id="disk-sub">Диск: --</div>
    </div>
  </div>

  <div class="section-title">Сервіси</div>
  <div class="services-grid" id="services-grid"></div>

  <div class="section-title">Мережевий статус</div>
  <div id="network-status-block" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">
    <div class="stat-card" id="ns-dmr" style="display:none">
      <div class="stat-label">DMR Мережа</div>
      <div style="color:var(--accent);font-size:13px;font-weight:600" id="ns-dmr-val">--</div>
    </div>
    <div class="stat-card" id="ns-ysf" style="display:none">
      <div class="stat-label">YSF Мережа</div>
      <div style="color:var(--accent);font-size:13px;font-weight:600" id="ns-ysf-val">--</div>
    </div>
    <div class="stat-card" id="ns-nxdn" style="display:none">
      <div class="stat-label">NXDN Мережа</div>
      <div style="color:var(--accent);font-size:13px;font-weight:600" id="ns-nxdn-val">--</div>
    </div>
    <div class="stat-card" id="ns-dstar" style="display:none">
      <div class="stat-label">D-Star Мережа</div>
      <div style="color:var(--accent);font-size:13px;font-weight:600" id="ns-dstar-val">--</div>
      <div style="color:var(--muted);font-size:11px" id="ns-dstar-irc"></div>
    </div>

  </div>
  <div class="section-title">Активність шлюзу<span id="today-count" style="flex:none;font-size:12px;letter-spacing:0;color:var(--accent);background:rgba(0,212,255,.1);border:1px solid var(--accent);border-radius:6px;padding:2px 10px;text-transform:none;margin-left:10px;display:none"></span></div>
  <div id="hourly-histogram" style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-bottom:12px;display:none">
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Передачі по годинах (сьогодні)</div>
    <div id="histo-bars" style="display:flex;align-items:flex-end;gap:2px;height:60px"></div>
    <div id="histo-labels" style="display:flex;gap:2px;margin-top:6px"></div>
  </div>
  <div id="mode-filter" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
    <button class="filter-btn active" data-filter="all" onclick="setModeFilter('all',this)">Всі</button>
    <button class="filter-btn" data-filter="YSF" onclick="setModeFilter('YSF',this)" style="color:#ffa500">YSF</button>
    <button class="filter-btn" data-filter="NXDN" onclick="setModeFilter('NXDN',this)" style="color:#4a9eff">NXDN</button>
    <button class="filter-btn" data-filter="DMR" onclick="setModeFilter('DMR',this)" style="color:#2dcc70">DMR</button>
    <button class="filter-btn" data-filter="D-Star" onclick="setModeFilter('D-Star',this)" style="color:#7a5eff">D-Star</button>
  </div>
  <div class="activity-panel" id="activity-panel">

    <div class="activity-table-wrap">
      <table class="activity-table">
        <thead><tr>
          <th class="col-date" style="color:var(--yellow)">Дата</th><th><span class="m-extra" style="display:none;color:var(--yellow)">Дата</span><span class="time-hdr" style="color:var(--text)">Час</span></th><th><span style="color:var(--yellow);display:block">Режим</span><span class="m-extra" style="display:none;color:var(--accent)">Позив.</span></th><th>Позивний</th><th class="col-target">Ціль</th><th><span style="color:var(--yellow);display:block">Напрям.</span><span class="m-extra" style="display:none;color:var(--text)">Трив.</span></th><th class="col-dur">Тривал.</th><th class="col-loss"><span style="color:var(--yellow);display:block">Втрати</span><span class="m-extra" style="display:none;color:var(--green)">BER</span></th><th class="col-ber">BER</th>
        </tr></thead>
        <tbody id="activity-body">
          <tr><td colspan="5" style="text-align:center;color:var(--muted)">Завантаження...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
  <div id="top-callsigns" style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:16px;display:none">
    <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Топ позивних (сьогодні)</div>
    <div id="top-list"></div>
  </div>
  <div class="section-title" id="logs-title" style="display:none">Журнали</div>
  <div class="logs-panel" id="logs-section" style="display:none">
    <div class="logs-header">
      <div class="logs-header-title">📋 Journal</div>
      <div class="logs-tabs" id="log-tabs"></div>
    </div>
    <div class="logs-body" id="logs-body">
      <div class="log-line">Оберіть сервіс для перегляду журналу...</div>
    </div>
  </div>
</div>

<!-- Login Modal -->
<div class="modal-overlay" id="login-modal">
  <div class="login-modal">
    <div class="login-title">🔐 Вхід для адміна</div>
    <div class="login-sub">MMDVM CONTROL CENTER // AUTH</div>
    <input type="password" class="input-field" id="login-pw" placeholder="Пароль адміна" onkeydown="if(event.key==='Enter')doLogin()">
    <div class="login-error" id="login-error">Невірний пароль</div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn btn-primary" style="flex:1" onclick="doLogin()">Увійти</button>
      <button class="btn" onclick="closeModal('login-modal')">Скасувати</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="chpass-modal">
  <div class="login-modal">
    <div class="login-title">&#128273; Зміна пароля</div>
    <div class="login-sub">MMDVM CONTROL CENTER // SECURITY</div>
    <input type="password" class="input-field" id="chpass-old" placeholder="Поточний пароль" style="margin-bottom:8px">
    <input type="password" class="input-field" id="chpass-new" placeholder="Новий пароль (мін. 6 символів)" style="margin-bottom:8px">
    <input type="password" class="input-field" id="chpass-confirm" placeholder="Підтвердіть новий пароль" onkeydown="if(event.key==='Enter')doChangePassword()">
    <div class="login-error" id="chpass-error"></div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn btn-primary" style="flex:1" onclick="doChangePassword()">Зберегти</button>
      <button class="btn" onclick="closeModal('chpass-modal')">Скасувати</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="chart-modal">
  <div class="modal" style="max-width:560px;width:92%">
    <div class="modal-header">
      <div style="font-size:16px;font-weight:600" id="chart-modal-title">Графік</div>
      <button class="modal-close" onclick="closeModal('chart-modal')">&#10005;</button>
    </div>
    <div style="padding:16px">
      <canvas id="metric-canvas" width="520" height="240" style="width:100%;height:auto;display:block"></canvas>
      <div id="chart-empty" style="text-align:center;color:var(--muted);font-size:13px;margin-top:10px;display:none">Дані ще накопичуються. Графік з'явиться згодом.</div>
      <div id="chart-info" style="text-align:center;color:var(--muted);font-size:12px;margin-top:10px"></div>
    </div>
  </div>
</div>

<!-- Service Action Modal -->
<div class="modal-overlay" id="service-modal">
  <div class="modal" style="max-width:320px">
    <div class="modal-header">
      <div style="font-size:16px;font-weight:600" id="service-modal-title">Сервіс</div>
      <button class="modal-close" onclick="closeModal('service-modal')">✕</button>
    </div>
    <div class="modal-body" style="padding:24px;display:flex;flex-direction:column;gap:12px">
      <button class="btn btn-primary" style="padding:12px" onclick="serviceModalRestart()">↺ Перезапустити</button>
      <button class="btn btn-danger" style="padding:12px" onclick="serviceModalStop()">■ Зупинити</button>
    </div>
  </div>
</div>

<!-- Configs Modal -->
<div class="modal-overlay" id="bmtg-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title">⭐ Статичні TG (Brandmeister)</div>
      <button class="modal-close" onclick="closeModal('bmtg-modal')">✕</button>
    </div>
    <div class="modal-body">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <div id="bmtg-info" style="font-size:12px;color:var(--muted)"></div>
        <button class="btn btn-sm" onclick="document.getElementById('bmtg-keybox').style.display='flex'">Змінити ключ</button>
      </div>
      <div id="bmtg-keybox" style="display:none;gap:8px;flex-direction:column">
        <div style="font-size:12px;color:var(--muted)">Введіть API-ключ Brandmeister (SelfCare → Profile settings → API Keys)</div>
        <input type="password" class="input-field" id="bmtg-key" placeholder="API ключ">
        <button class="btn btn-success" onclick="saveBmKey()">Зберегти ключ</button>
      </div>
      <div id="bmtg-list" style="display:flex;flex-direction:column;gap:6px"></div>
      <div style="display:flex;gap:8px;align-items:center">
        <select class="input-field" id="bmtg-slot" style="display:none;width:auto">
          <option value="1">TS1</option>
          <option value="2" selected>TS2</option>
        </select>
        <input type="text" class="input-field" id="bmtg-new" placeholder="Номер TG" style="flex:1">
        <button class="btn btn-primary" onclick="addBmTg()">Додати</button>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-sm" style="flex:1" onclick="bmAction('dropCallRoute')">Обірвати QSO</button>
        <button class="btn btn-sm" style="flex:1" onclick="bmAction('dropDynamicGroups')">Скинути динамічні</button>
      </div>
      <div id="bmtg-msg" style="font-size:12px"></div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="configs-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title">⚙️ Редактор конфігурацій</div>
      <button class="modal-close" onclick="closeModal('configs-modal')">✕</button>
    </div>
    <div class="modal-body">
      <div class="config-tabs" id="config-tabs"></div>
      <div class="config-path" id="config-path">Оберіть конфіг...</div>
      <textarea class="config-editor" id="config-editor" spellcheck="false" placeholder="Завантаження..."></textarea>
    </div>
    <div class="modal-footer">
      <div class="footer-info" id="config-info">Оберіть файл конфігурації зверху</div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-success" onclick="saveConfig()">💾 Зберегти</button>
        <button class="btn btn-primary" onclick="saveAndRestart()">💾 Зберегти та перезапустити</button>
      </div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="updates-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title">🔄 Оновлення сервісів</div>
      <button class="modal-close" onclick="closeModal('updates-modal')">✕</button>
    </div>
    <div class="modal-body">
      <div id="updates-list" style="font-family:var(--font-mono);font-size:14px;">Натисніть «Перевірити»...</div>
      <pre id="updates-output" style="display:none;margin-top:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;color:var(--text);max-height:300px;overflow:auto;white-space:pre-wrap;"></pre>
    </div>
    <div class="modal-footer">
      <div class="footer-info" id="updates-info">git-сервіси: MMDVMHost, YSFGateway, NXDNGateway</div>
      <button class="btn btn-primary" onclick="checkUpdates()">🔍 Перевірити</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const SERVICES = [
  {name:"mmdvmhost", label:"MMDVM Host", icon:"📡", port:"ttyAMA0"},
  {name:"dmrgateway", label:"DMR Gateway", icon:"💠", port:"62031"},
  {name:"ircddbgateway", label:"D-Star Gateway", icon:"⭐", port:"20010"},
  {name:"ysfgateway", label:"YSF Gateway", icon:"🔶", port:"4200"},
  {name:"nxdngateway", label:"NXDN Gateway", icon:"🔷", port:"14020"},
  {name:"ysfreflector", label:"YSF Reflector", icon:"🟠", port:"42000"},
  {name:"nxdnreflector", label:"NXDN Reflector", icon:"🔵", port:"41400"},
  {name:"mmdvm_bridge", label:"MMDVM Bridge", icon:"🌉", port:"62033"},
  {name:"analog_bridge", label:"Analog Bridge", icon:"🎵", port:"31100"},
  {name:"md380-emu", label:"AMBE Emulator", icon:"🔧", port:"2470"},
];

let activeLog = SERVICES[0].name;
let isAdmin = false;
let activeConfigId = null;
let currentConfigService = null;

function showToast(msg, type='success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.className = 'toast', 3000);
}

const UPD_LABELS = {mmdvmhost:"MMDVMHost", ysfgateway:"YSFGateway", nxdngateway:"NXDNGateway"};

function openUpdates() {
  document.getElementById('updates-output').style.display = 'none';
  document.getElementById('updates-list').innerHTML = 'Натисніть «Перевірити»...';
  document.getElementById('updates-modal').classList.add('open');
}

async function checkUpdates() {
  const list = document.getElementById('updates-list');
  list.innerHTML = '⚙️ Перевірка... (до 15 сек)';
  try {
    const r = await fetch('/api/updates/check');
    const data = await r.json();
    let html = '<table style="width:100%;border-collapse:collapse;">';
    for (const [name, info] of Object.entries(data)) {
      const label = UPD_LABELS[name] || name;
      let status, action;
      if (info.error) {
        status = '<span style="color:var(--red)">помилка</span>';
        action = '';
      } else if (info.behind > 0) {
        status = '<span style="color:#f0b000">⇳ відстає на ' + info.behind + '</span>';
        action = '<button class="btn btn-danger btn-sm" onclick="applyUpdate(\'' + name + '\')">Оновити</button>';
      } else {
        status = '<span style="color:#22c55e">✓ актуальна</span>';
        action = '';
      }
      html += '<tr style="border-bottom:1px solid var(--border);"><td style="padding:10px 8px;font-weight:700;">' + label + '</td><td style="padding:10px 8px;">' + status + '</td><td style="padding:10px 8px;text-align:right;">' + action + '</td></tr>';
    }
    html += '</table>';
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = '<span style="color:var(--red)">Помилка: ' + e + '</span>';
  }
}

async function applyUpdate(name) {
  const label = UPD_LABELS[name] || name;
  if (!confirm('Оновити ' + label + '?\n\nБуде зроблено бекап, git pull, компіляція та перезапуск.\nПри помилці — автоматичний відкат.')) return;
  const out = document.getElementById('updates-output');
  out.style.display = 'block';
  out.textContent = '⚙️ Оновлення ' + label + '... це може зайняти кілька хвилин, не закривайте сторінку.';
  try {
    const r = await fetch('/api/updates/apply/' + name, {method:'POST'});
    const data = await r.json();
    out.textContent = (data.output || data.error || 'no output');
    if (data.ok) {
      out.textContent += '\n\n✅ ГОТОВО';
    } else {
      out.textContent += '\n\n❌ НЕВДАЛО (відкат виконано), код=' + (data.code!==undefined?data.code:'?');
    }
    setTimeout(checkUpdates, 1000);
  } catch (e) {
    out.textContent = 'Помилка: ' + e;
  }
}


async function openBmTg() {
  document.getElementById('bmtg-modal').classList.add('open');
  document.getElementById('bmtg-msg').textContent = '';
  await loadBmTg();
}

async function loadBmTg() {
  const info = document.getElementById('bmtg-info');
  const list = document.getElementById('bmtg-list');
  const keybox = document.getElementById('bmtg-keybox');
  info.textContent = '\u0417\u0430\u0432\u0430\u043d\u0442\u0430\u0436\u0435\u043d\u043d\u044f...';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/bm/static');
    const d = await r.json();
    if (!d.ok) {
      info.textContent = d.error || '\u041f\u043e\u043c\u0438\u043b\u043a\u0430';
      keybox.style.display = 'flex';
      return;
    }
    keybox.style.display = d.haskey ? 'none' : 'flex';
    window._bmDuplex = !!d.duplex;
    document.getElementById('bmtg-slot').style.display = d.duplex ? '' : 'none';
    info.textContent = 'ID: ' + d.id + (d.haskey ? '' : ' \u2014 \u043a\u043b\u044e\u0447 \u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e');
    if (!d.static.length) {
      list.innerHTML = '<div style="color:var(--muted);font-size:12px">\u041d\u0435\u043c\u0430\u0454 \u0441\u0442\u0430\u0442\u0438\u0447\u043d\u0438\u0445 TG</div>';
      return;
    }
    d.static.forEach(function(x) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px';
      var slotTxt = window._bmDuplex ? ' <span style="color:var(--muted);font-size:11px">TS' + x.slot + '</span>' : '';
      row.innerHTML = '<span style="font-family:var(--font-mono);color:var(--accent)">TG ' + x.tg + '</span>' + slotTxt;
      const b = document.createElement('button');
      b.className = 'btn btn-sm btn-danger';
      b.textContent = '\u2715';
      b.onclick = function(){ delBmTg(x.tg, x.slot); };
      row.appendChild(b);
      list.appendChild(row);
    });
  } catch(e) {
    info.textContent = '\u041f\u043e\u043c\u0438\u043b\u043a\u0430: ' + e;
  }
}

async function saveBmKey() {
  const k = document.getElementById('bmtg-key').value.trim();
  const msg = document.getElementById('bmtg-msg');
  const r = await fetch('/api/bm/key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key:k})});
  const d = await r.json();
  if (d.ok) {
    document.getElementById('bmtg-key').value = '';
    msg.style.color = 'var(--green)';
    msg.textContent = '\u041a\u043b\u044e\u0447 \u0437\u0431\u0435\u0440\u0435\u0436\u0435\u043d\u043e';
    await loadBmTg();
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = d.error || '\u041f\u043e\u043c\u0438\u043b\u043a\u0430';
  }
}

async function addBmTg() {
  const tg = document.getElementById('bmtg-new').value.trim();
  const msg = document.getElementById('bmtg-msg');
  if (!tg) return;
  msg.style.color = 'var(--muted)';
  msg.textContent = '...';
  const r = await fetch('/api/bm/static', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tg:tg, slot: window._bmDuplex ? parseInt(document.getElementById('bmtg-slot').value) : 0})});
  const d = await r.json();
  if (d.ok) {
    document.getElementById('bmtg-new').value = '';
    msg.style.color = 'var(--green)';
    msg.textContent = 'TG ' + tg + ' \u0434\u043e\u0434\u0430\u043d\u043e';
    await loadBmTg();
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = JSON.stringify(d.result || d.error);
  }
}

async function bmAction(act) {
  const msg = document.getElementById('bmtg-msg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '...';
  const slot = window._bmDuplex ? parseInt(document.getElementById('bmtg-slot').value) : 0;
  const r = await fetch('/api/bm/action/' + act, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({slot:slot})});
  const d = await r.json();
  if (d.ok) {
    msg.style.color = 'var(--green)';
    msg.textContent = act === 'dropCallRoute' ? 'QSO обірвано' : 'Динамічні групи скинуто';
    await loadBmTg();
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = JSON.stringify(d.result || d.error);
  }
}

async function delBmTg(tg, slot) {
  const msg = document.getElementById('bmtg-msg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '...';
  const r = await fetch('/api/bm/static/' + tg + (slot !== undefined ? '?slot=' + slot : ''), {method:'DELETE'});
  const d = await r.json();
  if (d.ok) {
    msg.style.color = 'var(--green)';
    msg.textContent = 'TG ' + tg + ' \u0432\u0438\u0434\u0430\u043b\u0435\u043d\u043e';
    await loadBmTg();
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = JSON.stringify(d.result || d.error);
  }
}

function openAdmin() {
  if (isAdmin) return;
  document.getElementById('login-pw').value = '';
  document.getElementById('login-error').style.display = 'none';
  document.getElementById('login-modal').classList.add('open');
  setTimeout(() => document.getElementById('login-pw').focus(), 100);
}

function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}

async function doLogin() {
  const pw = document.getElementById('login-pw').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
  const data = await r.json();
  if (data.ok) {
    isAdmin = true;
    document.getElementById("logs-section").style.display = "block";
    document.getElementById("logs-title").style.display = "block";
    closeModal('login-modal');
    document.getElementById('admin-btn').style.display = 'none';
    document.getElementById('mmdvm-btn').style.display = '';
    document.getElementById('configs-btn').style.display = '';
    document.getElementById('updates-btn').style.display = '';
    document.getElementById('bmtg-btn').style.display = '';
    document.getElementById('logout-btn').style.display = '';
    document.getElementById('chpass-btn').style.display = '';
    showToast('✓ Авторизовано як адмін');
    showFailedAttempts(data.failed_attempts);
  } else {
    document.getElementById('login-error').style.display = 'block';
  }
}

function ackFailedAttempts() {
  var box = document.getElementById('auth-warn');
  if (box) box.remove();
  fetch('/api/login_attempts/clear', {method:'POST'}).catch(function(){});
}

function showFailedAttempts(list) {
  if (!list || !list.length) return;
  var old = document.getElementById('auth-warn');
  if (old) old.remove();

  var rows = list.slice(-15).reverse().map(function(a) {
    return '<div style="display:flex;justify-content:space-between;gap:14px;' +
           'padding:5px 0;border-bottom:1px solid rgba(255,255,255,.06)">' +
           '<span style="color:var(--text);font-family:ui-monospace,monospace">' + a.ip + '</span>' +
           '<span style="color:var(--muted);white-space:nowrap">' + a.time + '</span></div>';
  }).join('');

  var box = document.createElement('div');
  box.id = 'auth-warn';
  box.style.cssText = 'background:rgba(255,56,96,.08);border:1px solid rgba(255,56,96,.45);' +
    'border-radius:12px;padding:14px 16px;margin:0 0 16px';
  box.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px">' +
      '<span style="color:#ff3860;font-weight:700;font-size:14px">' +
        '&#9888; Невдалі спроби входу: ' + list.length + '</span>' +
      '<span onclick="ackFailedAttempts()" ' +
        'style="cursor:pointer;color:var(--muted);font-size:20px;line-height:1">&times;</span>' +
    '</div>' +
    '<div style="font-size:13px">' + rows + '</div>' +
    (list.length > 15 ? '<div style="color:var(--muted);font-size:12px;margin-top:6px">' +
      'показано останні 15</div>' : '');

  var host = document.querySelector('.main') || document.body;
  host.insertBefore(box, host.firstChild);
  window.scrollTo(0, 0);
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  isAdmin = false;
  document.getElementById("logs-section").style.display = "none";
  document.getElementById("logs-title").style.display = "none";
  document.getElementById('admin-btn').style.display = '';
  document.getElementById('mmdvm-btn').style.display = 'none';
  document.getElementById('configs-btn').style.display = 'none';
  document.getElementById('updates-btn').style.display = 'none';
  document.getElementById('logout-btn').style.display = 'none';
  document.getElementById('chpass-btn').style.display = 'none';
  showToast('Вийшли з режиму адміна', 'error');
}

async function doChangePassword() {
  var oldPw = document.getElementById('chpass-old').value;
  var newPw = document.getElementById('chpass-new').value;
  var confirmPw = document.getElementById('chpass-confirm').value;
  var errEl = document.getElementById('chpass-error');
  errEl.style.display = 'none';
  if (newPw.length < 6) {
    errEl.textContent = 'Новий пароль має містити щонайменше 6 символів';
    errEl.style.display = 'block';
    return;
  }
  if (newPw !== confirmPw) {
    errEl.textContent = 'Паролі не збігаються';
    errEl.style.display = 'block';
    return;
  }
  var r = await fetch('/api/change_password', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({old_password: oldPw, new_password: newPw})});
  var data = await r.json();
  if (data.ok) {
    closeModal('chpass-modal');
    document.getElementById('chpass-old').value = '';
    document.getElementById('chpass-new').value = '';
    document.getElementById('chpass-confirm').value = '';
    showToast('✓ Пароль змінено');
  } else {
    errEl.textContent = data.error || 'Помилка';
    errEl.style.display = 'block';
  }
}

async function openConfigs() {
  const r = await fetch('/api/configs');
  if (r.status === 401) { showToast('Потрібна авторизація', 'error'); return; }
  const data = await r.json();
  const tabs = document.getElementById('config-tabs');
  tabs.innerHTML = '';
  data.configs.forEach(c => {
    const btn = document.createElement('button');
    btn.className = 'config-tab';
    btn.id = 'ctab-' + c.id;
    btn.textContent = c.label;
    btn.onclick = () => loadConfig(c.id, c.path, c.service);
    if (!c.exists) btn.style.opacity = '0.4';
    tabs.appendChild(btn);
  });
  document.getElementById('configs-modal').classList.add('open');
  if (data.configs.length > 0) loadConfig(data.configs[0].id, data.configs[0].path, data.configs[0].service);
}

async function loadConfig(id, path, service) {
  activeConfigId = id;
  currentConfigService = service;
  document.querySelectorAll('.config-tab').forEach(t => t.classList.remove('active'));
  const tab = document.getElementById('ctab-' + id);
  if (tab) tab.classList.add('active');
  document.getElementById('config-path').textContent = path;
  document.getElementById('config-editor').value = 'Завантаження...';
  document.getElementById('config-info').textContent = path;
  const r = await fetch('/api/config/' + id);
  if (r.ok) {
    const data = await r.json();
    document.getElementById('config-editor').value = data.content;
  } else {
    document.getElementById('config-editor').value = '// Помилка завантаження файлу';
  }
}

async function saveConfig() {
  if (!activeConfigId) return;
  const content = document.getElementById('config-editor').value;
  const r = await fetch('/api/config/' + activeConfigId, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({content})});
  const data = await r.json();
  if (data.ok) showToast('✓ Збережено (резервна копія: .bak)');
  else showToast('Помилка: ' + data.error, 'error');
}

async function saveAndRestart() {
  await saveConfig();
  if (!currentConfigService) return;
  showToast('Перезапуск ' + currentConfigService + '...');
  const r = await fetch('/api/service/' + currentConfigService + '/restart', {method:'POST'});
  const data = await r.json();
  if (data.ok) {
    showToast('✓ Сервіс перезапущено');
    // Якщо змінили MMDVMHost конфіг - перезапускаємо dashboard для оновлення позивного/частоти
    if (currentConfigService === 'mmdvmhost') {
      setTimeout(async () => {
        await fetch('/api/service/dashboard/restart', {method:'POST'});
        setTimeout(() => location.reload(), 3000);
      }, 2000);
    }
  } else showToast('Помилка перезапуску', 'error');
}

async function restartService(name, e) {
  e.stopPropagation();
  const r = await fetch('/api/service/' + name + '/restart', {method:'POST'});
  const data = await r.json();
  if (data.ok) { showToast('✓ ' + name + ' перезапущено'); setTimeout(fetchStatus, 500); }
  else showToast('Помилка', 'error');
}

async function stopService(name, e) {
  e.stopPropagation();
  if (!confirm('Зупинити ' + name + '?')) return;
  const r = await fetch('/api/service/' + name + '/stop', {method:'POST'});
  const data = await r.json();
  if (data.ok) { showToast(name + ' зупинено', 'error'); setTimeout(fetchStatus, 500); }
  else showToast('Помилка', 'error');
}

function initUI() {
  const grid = document.getElementById('services-grid');
  const tabs = document.getElementById('log-tabs');
  SERVICES.forEach(s => {
    const card = document.createElement('div');
    card.className = 'service-card';
    card.id = 'card-' + s.name;
    card.innerHTML = `
      <div style="display:flex;flex-direction:column;width:100%">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <div class="service-icon">${s.icon}</div>
          <div class="service-name">${s.label}</div>
        </div>
        <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
          <div class="service-status" id="status-${s.name}">...</div>
          <span class="port-badge">:${s.port}</span>
        </div>
        <div class="service-meta" id="meta-${s.name}" style="margin-top:3px"></div>
      </div>
      <div class="service-actions" id="actions-${s.name}"></div>
    `;
    card.addEventListener('click', () => {
      if (isAdmin) { openServiceModal(s.name, s.label); } else { loadLogs(s.name); }
    });
    grid.appendChild(card);
    if (s.name !== 'md380-emu') {
      const tab = document.createElement('button');
      tab.className = 'log-tab' + (s.name === activeLog ? ' active' : '');
      tab.id = 'tab-' + s.name;
      tab.textContent = s.label;
      tab.addEventListener('click', () => loadLogs(s.name));
      tabs.appendChild(tab);
    }
  });
}

function updateAdminControls() {
  SERVICES.forEach(s => {
    const actions = document.getElementById('actions-' + s.name);
    if (!actions) return;
    if (isAdmin) {
      actions.innerHTML = `
        <button class="btn btn-sm btn-primary" onclick="restartService('${s.name}',event)">↺</button>
        <button class="btn btn-sm btn-danger" onclick="stopService('${s.name}',event)">■</button>
      `;
    } else {
      actions.innerHTML = '';
    }
  });
}

let lastUptimeUpdate = 0;
function openDiskChart() {
  var d = window._diskData;
  document.getElementById('chart-modal-title').textContent = 'Заповнення диску';
  document.getElementById('chart-modal').classList.add('open');
  var canvas = document.getElementById('metric-canvas');
  var emptyEl = document.getElementById('chart-empty');
  var infoEl = document.getElementById('chart-info');
  var ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  emptyEl.style.display = 'none';
  if (!d || !d.total) { emptyEl.style.display = 'block'; infoEl.textContent = ''; return; }
  var dpr = window.devicePixelRatio || 1;
  var W = 520, H = 240;
  var dispW = canvas.clientWidth || W;
  var dispH = canvas.clientHeight || H;
  canvas.width = Math.round(dispW * dpr);
  canvas.height = Math.round(dispH * dpr);
  ctx.setTransform(canvas.width / W, 0, 0, canvas.height / H, 0, 0);
  ctx.clearRect(0, 0, W, H);
  var cx = W/2, cy = H/2, R = Math.min(W,H)/2 - 30, thick = 34;
  var pct = d.percent / 100;
  var start = -Math.PI/2;
  // фон-кільце (вільне)
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI*2);
  ctx.strokeStyle = 'rgba(255,255,255,0.10)';
  ctx.lineWidth = thick;
  ctx.stroke();
  // зайнято
  var col = pct > 0.9 ? '#ff5757' : (pct > 0.75 ? '#ff9f43' : '#00d4ff');
  ctx.beginPath();
  ctx.arc(cx, cy, R, start, start + Math.PI*2*pct);
  ctx.strokeStyle = col;
  ctx.lineWidth = thick;
  ctx.lineCap = 'round';
  ctx.stroke();
  // текст у центрі
  ctx.fillStyle = col;
  ctx.font = 'bold 36px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(d.percent + '%', cx, cy - 6);
  ctx.fillStyle = '#7d8aa0';
  ctx.font = '13px monospace';
  ctx.fillText('зайнято', cx, cy + 22);
  infoEl.textContent = 'Зайнято ' + d.used + ' ГБ з ' + d.total + ' ГБ | вільно ' + (d.total - d.used) + ' ГБ';
}

async function openChart(metric) {
  var titles = {cpu: 'Завантаження ЦП за добу', mem: 'Память за добу', temp: 'Температура за добу'};
  var colors = {cpu: '#00d4ff', mem: '#7a5eff', temp: '#ff9f43'};
  var units = {cpu: '%', mem: '%', temp: '\u00b0C'};
  document.getElementById('chart-modal-title').textContent = titles[metric] || 'Графік';
  document.getElementById('chart-modal').classList.add('open');
  var canvas = document.getElementById('metric-canvas');
  var emptyEl = document.getElementById('chart-empty');
  var infoEl = document.getElementById('chart-info');
  var ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  infoEl.textContent = '';
  emptyEl.style.display = 'none';
  var data;
  try {
    var r = await fetch('/api/history');
    data = (await r.json()).history || [];
  } catch (e) { data = []; }
  var pts = data.map(function(d){ return {t: d.t, v: d[metric]}; }).filter(function(d){ return d.v !== null && d.v !== undefined; });
  // строго доба: лишаємо тільки точки за останні 24 год
  var cutoff = (Date.now() / 1000) - 86400;
  pts = pts.filter(function(d){ return d.t >= cutoff; });
  if (pts.length < 2) {
    emptyEl.style.display = 'block';
    return;
  }
  var dpr = window.devicePixelRatio || 1;
  var W = 520, H = 240;
  var dispW = canvas.clientWidth || W;
  var dispH = canvas.clientHeight || H;
  canvas.width = Math.round(dispW * dpr);
  canvas.height = Math.round(dispH * dpr);
  ctx.setTransform(canvas.width / W, 0, 0, canvas.height / H, 0, 0);
  ctx.clearRect(0, 0, W, H);
  var padL = 44, padR = 12, padT = 14, padB = 26;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var vals = pts.map(function(p){ return p.v; });
  var vmin = Math.min.apply(null, vals), vmax = Math.max.apply(null, vals);
  var vminReal = vmin;
  if (metric === 'cpu') {
    vmin = 0;
    vmax = Math.max(Math.ceil(vmax * 1.2), 5);
  } else if (metric === 'mem') {
    var pad = Math.max((vmax - vmin) * 0.3, 3);
    vmin = Math.max(0, Math.floor(vmin - pad));
    vmax = Math.ceil(vmax + pad);
  } else {
    vmin = Math.floor(vmin - 2);
    vmax = Math.ceil(vmax + 2);
  }
  if (vmax === vmin) vmax = vmin + 1;
  var t0 = pts[0].t, t1 = Math.floor(Date.now() / 1000);
  if (t1 <= t0) t1 = pts[pts.length-1].t;
  var tr = (t1 - t0) || 1;
  function px(t){ return padL + (t - t0) / tr * plotW; }
  function py(v){ return padT + (1 - (v - vmin) / (vmax - vmin)) * plotH; }
  // сітка + підписи Y
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.fillStyle = '#7d8aa0';
  ctx.font = '11px monospace';
  ctx.lineWidth = 1;
  for (var i = 0; i <= 4; i++) {
    var gv = vmin + (vmax - vmin) * i / 4;
    var gy = py(gv);
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(gv.toFixed(metric === 'temp' ? 0 : 0) + units[metric], padL - 6, gy + 4);
  }
  // підписи X (час) - початок, середина, кінець
  ctx.textAlign = 'center';
  var hrs = tr / 3600;
  var maxLabels = Math.max(2, Math.floor(plotW / 70));
  var stepCandidates = [1, 2, 3, 4, 6, 8, 12, 24];
  var stepH = 24;
  for (var si = 0; si < stepCandidates.length; si++) {
    if (hrs / stepCandidates[si] <= maxLabels) { stepH = stepCandidates[si]; break; }
  }
  var firstH = new Date(t0 * 1000);
  firstH.setMinutes(0, 0, 0);
  if (firstH.getTime() / 1000 < t0) firstH.setHours(firstH.getHours() + 1);
  while (firstH.getHours() % stepH !== 0) firstH.setHours(firstH.getHours() + 1);
  for (var ht = firstH.getTime() / 1000; ht <= t0 + tr; ht += stepH * 3600) {
    var fx = (ht - t0) / tr;
    if (fx < -0.001 || fx > 1.001) continue;
    var xpos = padL + plotW * fx;
    var dh = new Date(ht * 1000);
    var hh = ('0' + dh.getHours()).slice(-2) + ':00';
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xpos, padT);
    ctx.lineTo(xpos, padT + plotH);
    ctx.stroke();
    if (fx < 0.04) ctx.textAlign = 'left';
    else if (fx > 0.96) ctx.textAlign = 'right';
    else ctx.textAlign = 'center';
    ctx.fillText(hh, xpos, H - 8);
  }
  ctx.textAlign = 'center';
  // лінія
  ctx.strokeStyle = colors[metric];
  ctx.lineWidth = 2;
  ctx.beginPath();
  pts.forEach(function(p, idx){
    var x = px(p.t), y = py(p.v);
    if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  // заливка під лінією
  ctx.lineTo(px(pts[pts.length-1].t), padT + plotH);
  ctx.lineTo(px(pts[0].t), padT + plotH);
  ctx.closePath();
  ctx.fillStyle = colors[metric] + '22';
  ctx.fill();
  var cur = pts[pts.length-1].v;
  infoEl.textContent = 'Точок: ' + pts.length + ' | мін ' + vminReal.toFixed(1) + units[metric] + ' / макс ' + Math.max.apply(null,vals).toFixed(1) + units[metric] + ' / зараз ' + cur.toFixed(1) + units[metric];
}

function animateValue(id, to, suffix='', duration=800) {
  const el = document.getElementById(id);
  if (!el) return;
  const from = parseFloat(el.textContent) || 0;
  if (Math.abs(from - to) < 0.1) { el.textContent = to.toFixed(1) + suffix; return; }
  const start = performance.now();
  function step(now) {
    const p = Math.min((now - start) / duration, 1);
    const ease = p < 0.5 ? 2*p*p : -1+(4-2*p)*p;
    el.textContent = (from + (to - from) * ease).toFixed(1) + suffix;
    if (p < 1) requestAnimationFrame(step);
    else el.textContent = to.toFixed(1) + suffix;
  }
  requestAnimationFrame(step);
}
function updateServices(data) {
  // годинник оновлюється окремим таймером щосекунди
  if (data.admin && !isAdmin) {
    isAdmin = true;
    document.getElementById('admin-btn').style.display = 'none';
    document.getElementById('configs-btn').style.display = '';
    document.getElementById('updates-btn').style.display = '';
    document.getElementById('logs-section').style.display = 'block';
    document.getElementById('logout-btn').style.display = '';
    document.getElementById('chpass-btn').style.display = '';
  }
  const sys = data.system;
  animateValue('cpu', parseFloat(sys.cpu), '%');
  document.getElementById('cpu-bar').style.width = sys.cpu + '%';
  animateValue('mem', parseFloat(sys.mem_percent), '%');
  document.getElementById('mem-sub').textContent = sys.mem_used + ' / ' + sys.mem_total + ' MB';
  document.getElementById('mem-bar').style.width = sys.mem_percent + '%';
  document.getElementById('temp').textContent = sys.temp || '--';
  if (!lastUptimeUpdate || Date.now() - lastUptimeUpdate > 59000) {
    document.getElementById('uptime').textContent = sys.uptime;
    lastUptimeUpdate = Date.now();
  }
  document.getElementById('disk-sub').textContent = `Диск: ${sys.disk_used}/${sys.disk_total}ГБ (${sys.disk_percent}%)`; window._diskData = {used: sys.disk_used, total: sys.disk_total, percent: sys.disk_percent};
  data.services.forEach(s => {
    const card = document.getElementById('card-' + s.name);
    const status = document.getElementById('status-' + s.name);
    const meta = document.getElementById('meta-' + s.name);
    if (!card) return;
    card.className = 'service-card ' + (s.active ? 'active' : 'inactive');
    status.className = 'service-status ' + (s.active ? 'up' : 'down');
    status.textContent = s.active ? '● ОНЛАЙН' : '● ОФЛАЙН';
    meta.textContent = s.active ? (s.uptime ? 'запущено ' + s.uptime : '') + (s.memory ? ' · ' + s.memory : '') : ('зупинено' + (s.stopped_ago ? ' ' + s.stopped_ago + ' тому' : ''));
  });
  updateAdminControls();
}

function colorLine(line) {
  if (line.includes(' E: ') || line.includes('Error') || line.includes('error') || line.includes('FAILED')) return 'error';
  if (line.includes(' W: ') || line.includes('Warning') || line.includes('warn')) return 'warn';
  if (line.includes(' M: ') || line.includes('started') || line.includes('running') || line.includes('Connected') || line.includes('Logged')) return 'ok';
  return 'info';
}

function loadLogs(name) {
  activeLog = name;
  document.querySelectorAll('.log-tab').forEach(t => t.classList.remove('active'));
  const tab = document.getElementById('tab-' + name);
  if (tab) tab.classList.add('active');
  fetch('/api/logs/' + name)
    .then(r => r.json())
    .then(data => {
      const body = document.getElementById('logs-body');
      body.innerHTML = data.lines.map(l => `<div class="log-line ${colorLine(l)}">${l.replace(/</g,'&lt;')}</div>`).join('');
      body.scrollTop = body.scrollHeight;
    });
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
  }
});

let _serviceModalName = null;
function openServiceModal(name, label) {
  _serviceModalName = name;
  document.getElementById('service-modal-title').textContent = label;
  document.getElementById('service-modal').classList.add('open');
}
function serviceModalRestart() {
  if (_serviceModalName) {
    closeModal('service-modal');
    const fakeEvent = { stopPropagation: function(){} };
    restartService(_serviceModalName, fakeEvent);
  }
}
function serviceModalStop() {
  if (_serviceModalName) {
    closeModal('service-modal');
    const fakeEvent = { stopPropagation: function(){} };
    stopService(_serviceModalName, fakeEvent);
  }
}
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    updateServices(data);
  } catch(e) {}
}
function updateNetworkStatus(d) {
  if (d.dmr_enabled === '1') {
    document.getElementById('ns-dmr').style.display = '';
    document.getElementById('ns-dmr-val').textContent = d.dmr_master || '--';
  }
  if (d.ysf_enabled === '1') {
    document.getElementById('ns-ysf').style.display = '';
    document.getElementById('ns-ysf-val').textContent = d.ysf_linked || '--';
  }
  if (d.nxdn_enabled === '1') {
    document.getElementById('ns-nxdn').style.display = '';
    document.getElementById('ns-nxdn-val').textContent = d.nxdn_linked || '--';
  }
  if (d.dstar_enabled === '1') {
    document.getElementById('ns-dstar').style.display = '';
    document.getElementById('ns-dstar-val').textContent = d.dstar_linked || '--';
    document.getElementById('ns-dstar-irc').textContent = '';
  }
}
async function fetchNetworkStatus() {
  try {
    const r = await fetch('/api/network_status');
    const d = await r.json();
    if (d.dmr_enabled === '1') {
      document.getElementById('ns-dmr').style.display = '';
      document.getElementById('ns-dmr-val').textContent = d.dmr_master || '--';
    }
    if (d.ysf_enabled === '1') {
      document.getElementById('ns-ysf').style.display = '';
      document.getElementById('ns-ysf-val').textContent = d.ysf_linked || '--';
    }
    if (d.nxdn_enabled === '1') {
      document.getElementById('ns-nxdn').style.display = '';
      document.getElementById('ns-nxdn-val').textContent = d.nxdn_linked || '--';
    }
    if (d.dstar_enabled === '1') {
      document.getElementById('ns-dstar').style.display = '';
      document.getElementById('ns-dstar-val').textContent = d.dstar_linked || '--';
      document.getElementById('ns-dstar-irc').textContent = '';
    }
  } catch(e) {}
}

initUI();
fetchStatus();
function tickClock(){ document.getElementById('clock').textContent = new Date().toLocaleTimeString('uk-UA', {hour12:false}); }
tickClock();
setInterval(tickClock, 1000);
setInterval(() => { if (activeLog) loadLogs(activeLog); }, 15000);

// SSE real-time stream для системних метрик

let sseSource = null;
function startSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource('/api/stream');
  sseSource.onmessage = function(e) {
    try {
      const sys = JSON.parse(e.data);
      if (!sys.cpu && sys.cpu !== 0) return;
      if (!window._cpuMemTs || Date.now() - window._cpuMemTs > 3000) {
        window._cpuMemTs = Date.now();
        animateValue('cpu', parseFloat(sys.cpu), '%', 3000);
        document.getElementById('cpu-bar').style.width = sys.cpu + '%';
        animateValue('mem', parseFloat(sys.mem_percent), '%', 3000);
        document.getElementById('mem-sub').textContent = sys.mem_used + ' / ' + sys.mem_total + ' MB';
        document.getElementById('mem-bar').style.width = sys.mem_percent + '%';
      }
      document.getElementById('temp').textContent = sys.temp || '--';
      if (!lastUptimeUpdate || Date.now() - lastUptimeUpdate > 59000) {
        document.getElementById('uptime').textContent = sys.uptime;
        lastUptimeUpdate = Date.now();
      }
      document.getElementById('disk-sub').textContent = `Диск: ${sys.disk_used}/${sys.disk_total}ГБ (${sys.disk_percent}%)`; window._diskData = {used: sys.disk_used, total: sys.disk_total, percent: sys.disk_percent};
      if (sys.activity) updateActivity(sys.activity);
      if (sys.hourly) { lastHourly = sys.hourly; updateHistogram(lastHourly); }
      updateTodayCount();
      if (sys.top) { lastTop = sys.top; updateTopCallsigns(lastTop); }
      if (sys.network) updateNetworkStatus(sys.network);
      // TRX статус - підсвічуємо заголовки мережевих плиток
      if (sys.network) {
        var trxStatus = sys.network.trx_status || 'Listening';
        var trxMode = sys.network.trx_mode || '';
        var isTx = trxStatus.startsWith('TX');
        var isListeningMode = trxStatus.startsWith('Listening') && trxMode;
        // Мапа режим -> id плитки
        var modeMap = {
          'DMR': 'ns-dmr', 'YSF': 'ns-ysf',
          'NXDN': 'ns-nxdn', 'D-Star': 'ns-dstar'
        };
        // Скидаємо всі заголовки
        ['ns-dmr','ns-ysf','ns-nxdn','ns-dstar'].forEach(function(id) {
          var lbl = document.querySelector('#' + id + ' .stat-label');
          if (lbl) lbl.style.color = '';
        });
        // Підсвічуємо активний режим
        if (trxMode && modeMap[trxMode]) {
          var activeLbl = document.querySelector('#' + modeMap[trxMode] + ' .stat-label');
          if (activeLbl) {
            // TX або Listening з режимом - червоний
            activeLbl.style.color = '#ff3860';
          }
        } else if (!trxMode) {
          // Listening без режиму - всі жовті
          ['ns-dmr','ns-ysf','ns-nxdn','ns-dstar'].forEach(function(id) {
            var lbl = document.querySelector('#' + id + ' .stat-label');
            if (lbl) lbl.style.color = 'var(--muted)';
          });
        }
      }

      // Підсвічуємо плитку Network Status при активному TX
      var activeMode = null;
      if (sys.activity) {
        var act = sys.activity.find(function(e) { return e.active; });
        if (act) activeMode = act.mode;
      }
      ['ns-dmr','ns-ysf','ns-nxdn','ns-dstar'].forEach(function(id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.classList.remove('tx-active');
      });
      if (activeMode) {
        var map = {'DMR':'ns-dmr','DMR Slot 1':'ns-dmr','DMR Slot 2':'ns-dmr','YSF':'ns-ysf','NXDN':'ns-nxdn','D-Star':'ns-dstar'};
        var tid = map[activeMode];
        if (tid) {
          var el = document.getElementById(tid);
          if (el) el.classList.add('tx-active');
        }
      }
    } catch(err) {}
  };
  sseSource.onerror = function() {
    sseSource.close();
    setTimeout(startSSE, 5000);
  };
}
startSSE();
fetchStatus();
fetchNetworkStatus();
setInterval(fetchStatus, 15000);
// fetchNetworkStatus оновлюється через SSE


function getModeClass(mode) {
  if (mode.startsWith('DMR')) return 'mode-dmr';
  if (mode === 'YSF') return 'mode-ysf';
  if (mode === 'D-Star') return 'mode-dstar';
  if (mode === 'NXDN') return 'mode-nxdn';
  if (mode === 'P25') return 'mode-p25';
  return 'mode-dmr';
}
function updateTopCallsigns(data) {
  var box = document.getElementById('top-callsigns');
  var list = document.getElementById('top-list');
  if (!box || !list) return;
  var top = Array.isArray(data) ? data : (data[currentModeFilter] || data['all'] || []);
  if (!top || top.length === 0) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  var max = top[0].count || 1;
  var html = '';
  for (var i = 0; i < top.length; i++) {
    var t = top[i];
    var pct = Math.round(t.count / max * 100);
    var rankCol = i === 0 ? 'var(--accent)' : (i < 3 ? 'var(--green)' : 'var(--muted)');
    var pureCs = (t.callsign || '').split(/[ /]/)[0];
    var link = '<a href="https://database.radioid.net/database/view?callsign=' + encodeURIComponent(pureCs) + '" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none;font-weight:600">' + t.callsign + '</a>';
    html += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">';
    html += '<span style="width:22px;text-align:right;color:' + rankCol + ';font-weight:700;font-size:13px">' + (i+1) + '</span>';
    html += '<span style="width:90px">' + link + '</span>';
    html += '<span style="flex:1;height:8px;background:var(--border);border-radius:4px;overflow:hidden"><span style="display:block;height:100%;width:' + pct + '%;background:var(--green);border-radius:4px"></span></span>';
    html += '<span style="width:36px;text-align:right;color:var(--text);font-size:13px">' + t.count + '</span>';
    html += '</div>';
  }
  list.innerHTML = html;
}

function updateTodayCount() {
  var tc = document.getElementById('today-count');
  if (!tc) return;
  var n = 0;
  if (lastHourly) {
    var arr = lastHourly[currentModeFilter] || lastHourly['all'] || [];
    for (var i = 0; i < arr.length; i++) n += arr[i];
  }
  if (n > 0) { tc.textContent = 'сьогодні: ' + n; tc.style.display = 'inline-block'; }
  else { tc.style.display = 'none'; }
}

function updateHistogram(data) {
  var box = document.getElementById('hourly-histogram');
  var bars = document.getElementById('histo-bars');
  if (!box || !bars) return;
  var hours = Array.isArray(data) ? data : (data[currentModeFilter] || data['all'] || []);
  if (!hours.length) { box.style.display = 'none'; return; }
  var total = hours.reduce(function(a,b){return a+b;}, 0);
  if (total === 0) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  var max = Math.max.apply(null, hours);
  var curHour = new Date().getHours();
  var html = '';
  for (var h = 0; h < 24; h++) {
    var cnt = hours[h];
    var pct = max > 0 ? Math.round(cnt / max * 100) : 0;
    var isCur = (h === curHour);
    var col = isCur ? 'var(--accent)' : (cnt > 0 ? 'var(--green)' : 'var(--border)');
    var minH = cnt > 0 ? 4 : 2;
    html += '<div style="flex:1;display:flex;align-items:flex-end;height:100%" title="' + h + ':00 — ' + cnt + '">';
    html += '<div style="width:100%;height:' + Math.max(pct, minH) + '%;background:' + col + ';border-radius:2px 2px 0 0;transition:height .3s"></div>';
    html += '</div>';
  }
  bars.innerHTML = html;
  // Окремий рядок підписів годин під стовпчиками (кожна колонка flex:1, як стовпчики)
  var labels = document.getElementById('histo-labels');
  if (labels) {
    var lhtml = '';
    for (var h2 = 0; h2 < 24; h2++) {
      lhtml += '<div style="flex:1;text-align:center;font-size:9px;color:var(--muted)">' + (h2 % 3 === 0 ? h2 : '') + '</div>';
    }
    lhtml += '<div style="width:0;text-align:right;font-size:9px;color:var(--muted);transform:translateX(-50%)">24</div>';
    labels.innerHTML = lhtml;
  }
}

var currentModeFilter = 'all';
var lastHourly = null;
var lastTop = null;
function setModeFilter(mode, btn) {
  currentModeFilter = mode;
  var btns = document.querySelectorAll('#mode-filter .filter-btn');
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
  if (btn) btn.classList.add('active');
  fetchFiltered(true);
  if (lastHourly) updateHistogram(lastHourly);
  if (lastTop) updateTopCallsigns(lastTop);
  updateTodayCount();
}
function applyModeFilter() {
  var rows = document.querySelectorAll('#activity-body tr');
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    var m = rows[i].getAttribute('data-mode');
    var match = (currentModeFilter === 'all' || m === currentModeFilter);
    if (match && shown < 20) {
      rows[i].style.display = '';
      shown++;
    } else {
      rows[i].style.display = 'none';
    }
  }
}

var _lastFilterFetch = 0;
function fetchFiltered(force) {
  var now = Date.now();
  if (!force && now - _lastFilterFetch < 2000) return;
  _lastFilterFetch = now;
  fetch('/api/activity?mode=' + encodeURIComponent(currentModeFilter) + '&limit=20')
    .then(function(r) { return r.json(); })
    .then(function(d) { renderRows(d.activity || []); })
    .catch(function(e) { console.error('fetchFiltered error:', e); });
}
function updateActivity(activity) {
  if (currentModeFilter !== 'all') { fetchFiltered(); return; }
  renderRows(activity);
}
function renderRows(activity) {
  const tbody = document.getElementById('activity-body');
  if (!tbody || !activity) return;

  var cols = 5;
  if (activity.length === 0) {
    tbody.innerHTML = '<tr><td colspan="' + cols + '" style="text-align:center;color:var(--muted)">Немає даних</td></tr>';
    return;
  }
  try {
    var rows = activity.map(function(e) {
    function callsignLink(cs, active) {
      var style = 'color:var(--accent);font-weight:' + (active?'600':'400') + ';text-decoration:none';
      if (!cs) return '';
      var pureCs = cs.split(/[ /]/)[0];
      return '<a href="https://database.radioid.net/database/view?callsign=' + encodeURIComponent(pureCs) + '" target="_blank" rel="noopener" style="' + style + '">' + cs + '</a>';
    }
      var isActive = e.active === true;
      var rowStyle = isActive ? 'background:rgba(255,56,96,0.08);border-left:3px solid #ff3860;' : '';
      var srcClass = e.src === 'RF' ? 'src-rf' : (e.src === 'LNet' || e.src.indexOf('↑') >= 0) ? 'src-lnet' : 'src-net';
      var txBadge = isActive ? ' <span style="color:#ff3860;font-size:11px;font-weight:700">&#9679; TX</span>' : '';
      // Формуємо дату з epoch
      var dateStr = '';
      if (e.epoch) {
        var d = new Date(e.epoch * 1000);
        dateStr = d.toLocaleDateString('uk-UA', {day:'2-digit', month:'2-digit'});
      }
      var baseMode = e.mode.indexOf('DMR') === 0 ? 'DMR' : e.mode;
      return '<tr data-mode="' + baseMode + '" style="' + rowStyle + '">' +
        '<td class="col-date" style="color:var(--yellow)">' + dateStr + '</td>' +
        '<td><span class="m-extra" style="font-size:12px;color:var(--yellow)">' + dateStr + '</span><span style="color:var(--text)">' + e.time + '</span></td>' +
        '<td><span class="mode-badge ' + getModeClass(e.mode) + '">' + e.mode + '</span>' +
          '<span class="m-extra" style="margin-top:3px">' + callsignLink(e.callsign, isActive) + txBadge + '</span></td>' +
        '<td>' + (e.flag ? e.flag + ' ' : '') + callsignLink(e.callsign, isActive) + txBadge + '</td>' +
        '<td class="col-target">' + e.target + '</td>' +
        '<td><span class="' + srcClass + '">' + e.src + '</span>' +
          '<span class="m-extra" style="margin-top:3px;font-size:11px;color:var(--text)">' + (e.dur||'---') + '</span></td>' +
        '<td class="col-dur" style="font-size:13px;color:var(--muted)">' + (e.dur||'---') + '</td>' +
        '<td class="col-loss" style="font-size:13px;color:var(--yellow)">' + (e.loss||'---') +
          '<span class="m-extra" style="margin-top:3px;color:' + (e.ber && e.ber !== '---' && parseFloat(e.ber) > 1 ? '#ff3860' : 'var(--green)') + '">' + (e.ber||'---') + '</span></td>' +
        '<td class="col-ber" style="font-size:13px;color:' + (e.ber && e.ber !== '---' && parseFloat(e.ber) > 1 ? '#ff3860' : 'var(--green)') + '">' + (e.ber||'---') + '</td>' +
        '</tr>';
    });
    tbody.innerHTML = rows.join('');
    if (currentModeFilter === 'all') applyModeFilter();
  } catch(ex) { console.error('updateActivity error:', ex); }
}
</script>
</body>
</html>
"""


@app.route("/api/network_status")
def api_network_status():
    import configparser, glob as _gl
    result = {}

    # --- General (MMDVMHost) ---
    mmdvm_ini = "/opt/MMDVMHost/MMDVM-Host.ini"
    try:
        cp = configparser.RawConfigParser()
        cp.read(mmdvm_ini)
        result["callsign"] = cp.get("General", "Callsign", fallback="--")
        result["dmr_id"] = cp.get("General", "Id", fallback="--")
        result["dmr_enabled"] = cp.get("DMR", "Enable", fallback="0")
        result["ysf_enabled"] = cp.get("System Fusion Network", "Enable", fallback="0")
        result["nxdn_enabled"] = cp.get("NXDN Network", "Enable", fallback="0")
        result["dstar_enabled"] = cp.get("D-Star Network", "Enable", fallback="0")
    except: pass

    # --- DMR Master (з конфігу MMDVMHost) ---
    try:
        import configparser as _cp2
        cp2 = _cp2.RawConfigParser()
        cp2.read("/opt/MMDVMHost/MMDVM-Host.ini")
        dmr_net_enabled = cp2.get("DMR Network", "Enable", fallback="0")
        if dmr_net_enabled == "1":
            gw_addr = cp2.get("DMR Network", "GatewayAddress", fallback="")
            names = []
            try:
                gw2 = _cp2.RawConfigParser(strict=False)
                gw2.read("/opt/DMRGateway/DMRGateway.ini")
                for sec in gw2.sections():
                    if sec.startswith("DMR Network") and gw2.get(sec, "Enabled", fallback="0") == "1":
                        nm = gw2.get(sec, "Name", fallback="").strip()
                        if nm:
                            names.append(nm)
            except Exception:
                pass
            if names:
                result["dmr_master"] = ", ".join(names)
            elif gw_addr:
                result["dmr_master"] = gw_addr
            else:
                result["dmr_master"] = "Не підключено"
        else:
            result["dmr_master"] = "Вимкнено"
    except: result["dmr_master"] = "--"

    # --- YSF (з MQTT кешу) ---
    result["ysf_linked"] = _network_status_cache.get("ysf_linked", "Не підключено")

    # --- NXDN (з MQTT кешу) ---
    result["nxdn_linked"] = _network_status_cache.get("nxdn_linked", "Не підключено")

    # --- D-Star (з кешу) ---
    try:
        dstar = {}
        with open("/etc/ircddbgateway") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    dstar[k.strip()] = v.strip().strip('"')
        result["dstar_irc"] = dstar.get("ircddbHostname", "--")[:16]
    except: result["dstar_irc"] = "--"
    result["dstar_linked"] = _network_status_cache.get("dstar_linked", "Не підключено")

    return jsonify(result)


# ---------- BrandMeister API ----------
BM_KEY_FILE = "/opt/dashboard/bm_apikey"
BM_API = "https://api.brandmeister.network/v2/device"

def _bm_key():
    try:
        with open(BM_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def _bm_id():
    try:
        import configparser as _cpb
        c = _cpb.RawConfigParser(strict=False)
        c.read("/opt/DMRGateway/DMRGateway.ini")
        for sec in c.sections():
            if sec.startswith("DMR Network") and c.get(sec, "Enabled", fallback="0") == "1":
                if "brandmeister" in c.get(sec, "Name", fallback="").lower():
                    return c.get(sec, "Id", fallback="").strip()
    except Exception:
        pass
    return ""

def _bm_duplex():
    """True, якщо хотспот дуплексний (BM тоді очікує слот 1 або 2)."""
    try:
        import configparser as _cpd
        c = _cpd.RawConfigParser(strict=False)
        c.read("/opt/DMRGateway/DMRGateway.ini")
        return c.get("Info", "Duplex", fallback="1").strip() == "1"
    except Exception:
        return False

def _bm_req(method, path, body=None):
    import urllib.request, urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BM_API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    k = _bm_key()
    if k:
        req.add_header("Authorization", "Bearer " + k)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(txt)
            except Exception:
                return True, txt
    except urllib.error.HTTPError as e:
        return False, e.read().decode("utf-8", "ignore")[:200]
    except Exception as e:
        return False, str(e)

@app.route("/api/bm/key")
@login_required
def api_bm_key_status():
    return jsonify({"haskey": bool(_bm_key()), "id": _bm_id()})

@app.route("/api/bm/key", methods=["POST"])
@login_required
def api_bm_key_save():
    k = (request.json.get("key") or "").strip()
    if len(k) < 20:
        return jsonify({"ok": False, "error": "Ключ закороткий"})
    try:
        with open(BM_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(k)
        os.chmod(BM_KEY_FILE, 0o600)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/bm/static")
@login_required
def api_bm_static():
    bid = _bm_id()
    if not bid:
        return jsonify({"ok": False, "error": "Brandmeister не налаштований"})
    ok, res = _bm_req("GET", "/%s/profile" % bid)
    if not ok:
        return jsonify({"ok": False, "error": res, "id": bid})
    tgs = []
    if isinstance(res, dict):
        for x in res.get("staticSubscriptions", []):
            tgs.append({"tg": x.get("talkgroup"), "slot": x.get("slot")})
    return jsonify({"ok": True, "id": bid, "static": tgs, "haskey": bool(_bm_key()), "duplex": _bm_duplex()})

@app.route("/api/bm/action/<act>", methods=["POST"])
@login_required
def api_bm_action(act):
    if act not in ("dropCallRoute", "dropDynamicGroups"):
        return jsonify({"ok": False, "error": "Невідома дія"})
    bid = _bm_id()
    if not bid:
        return jsonify({"ok": False, "error": "Brandmeister не налаштований"})
    slot = request.json.get("slot") if request.json else None
    if slot is None:
        slot = 2 if _bm_duplex() else 0
    ok, res = _bm_req("GET", "/%s/action/%s/%s" % (bid, act, int(slot)))
    return jsonify({"ok": ok, "result": res})

@app.route("/api/bm/static", methods=["POST"])
@login_required
def api_bm_add():
    bid = _bm_id()
    tg = str(request.json.get("tg", "")).strip()
    if not bid or not tg.isdigit():
        return jsonify({"ok": False, "error": "Невірні дані"})
    slot = request.json.get("slot")
    if slot is None:
        slot = 2 if _bm_duplex() else 0
    ok, res = _bm_req("POST", "/%s/talkgroup" % bid, {"slot": int(slot), "group": int(tg)})
    return jsonify({"ok": ok, "result": res})

@app.route("/api/bm/static/<tg>", methods=["DELETE"])
@login_required
def api_bm_del(tg):
    bid = _bm_id()
    if not bid or not tg.isdigit():
        return jsonify({"ok": False, "error": "Невірні дані"})
    slot = request.args.get("slot")
    if slot is None:
        slot = 2 if _bm_duplex() else 0
    ok, res = _bm_req("DELETE", "/%s/talkgroup/%s/%s" % (bid, int(slot), tg))
    return jsonify({"ok": ok, "result": res})

@app.route("/api/callsign/<ids>")
def api_callsign(ids):
    """DMR ID -> позивний. Кілька ID через кому: /api/callsign/1234567,7654321
    Потрібен Live-сторінці: вона отримує дані з MQTT напряму,
    минаючи Flask, тому доступу до бази позивних не має."""
    out = {}
    for raw in ids.split(",")[:50]:
        raw = raw.strip()
        if not raw:
            continue
        if raw.isdigit():
            cs = dmr_id_to_callsign(raw)
            out[raw] = {"callsign": cs if cs != raw else "",
                        "flag": _dmr_flag(raw) or (_call_flag(cs) if cs != raw else "")}
        else:
            # Позивний напряму (YSF/NXDN/D-Star) - потрібен лише прапорець
            out[raw] = {"callsign": raw, "flag": _call_flag(raw)}
    return jsonify(out)


@app.route("/api/activity")
def api_activity():
    mode = (request.args.get("mode") or "").strip()
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 10000))
    rows = _activity_cache
    if not rows:
        rows = _scan_today_log()
    if mode and mode.lower() != "all":
        if mode.upper() == "DMR":
            rows = [e for e in rows if e.get("mode", "").startswith("DMR")]
        else:
            rows = [e for e in rows if e.get("mode", "") == mode]
    return jsonify({"activity": rows[:limit], "today_count": _count_today_qso(),
                    "total": len(rows), "mode": mode or "all"})


@app.route("/api/history")
def api_history():
    return jsonify({"history": _metric_history})


@app.route("/api/stream")
def api_stream():
    def generate():
        while True:
            try:
                sys_data = {k: v for k, v in _system_cache.items()}
                act_data = list(_activity_cache[:100])
                sys_data['activity'] = act_data
                global _today_count, _today_count_ts, _hourly_cache, _top_cache
                if time.time() - _today_count_ts > 10:
                    _today_count = _count_today_qso()
                    _hourly_cache = _hourly_activity()
                    _top_cache = _top_callsigns()
                    _today_count_ts = time.time()
                sys_data['today_count'] = _today_count
                sys_data['hourly'] = _hourly_cache
                sys_data['top'] = _top_cache
                sys_data['network'] = dict(_network_status_cache)
                data = json.dumps(sys_data, ensure_ascii=False, default=str)
                yield f"data: {data}\n\n"
            except Exception as e:
                yield f"data: {{\"err\": \"{ str(e)[:50] }\"}}\n\n"
            time.sleep(1)
    return Response(generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _save_history_on_exit(signum=None, frame=None):
    try:
        with open(_METRIC_HISTORY_FILE, "w", encoding="utf-8") as _hf:
            json.dump(_metric_history, _hf)
    except Exception:
        pass
    if signum is not None:
        import sys as _sys
        _sys.exit(0)

if __name__ == "__main__":
    import signal as _signal
    _signal.signal(_signal.SIGTERM, _save_history_on_exit)
    _signal.signal(_signal.SIGINT, _save_history_on_exit)
    try:
        app.run(host="0.0.0.0", port=85, debug=False, threaded=True)
    finally:
        _save_history_on_exit()
