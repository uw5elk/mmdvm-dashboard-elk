#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MMDVM State Bridge
Слухає mmdvm/json (події реального часу) і публікує поточний стан
"хто зараз в ефірі" у топік mmdvm/state з прапорцем retained.
Це дозволяє новим клієнтам (Live-сторінка) одразу бачити активність
при завантаженні, не чекаючи наступної події.
"""
import json
import time
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883
SRC_TOPIC = "mmdvm/json"
STATE_TOPIC = "mmdvm/state"

# Ідентифікатори для підстановки позивних (як на Live-сторінці)
NXDN_IDS = {}  # напр. {"1234": "MYCALL"} - зіставлення NXDN ID -> позивний
DMR_IDS = {}  # напр. {"1234567": "MYCALL"} - зіставлення DMR ID -> позивний

def publish_state(client, state):
    """Публікує поточний стан як retained."""
    client.publish(STATE_TOPIC, json.dumps(state), retain=True)

def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe(SRC_TOPIC)
    # При старті - публікуємо 'тиша' як початковий стан
    publish_state(client, {"active": False})

def on_message(client, userdata, msg):
    try:
        o = json.loads(msg.payload.decode("utf-8", "ignore"))
    except Exception:
        return

    # Режим idle -> тиша
    if "MMDVM" in o:
        mode = o["MMDVM"].get("mode", "")
        if mode == "idle":
            publish_state(client, {"active": False})
        return

    # Події передач по режимах
    for mode in ["NXDN", "DMR", "YSF", "D-Star", "DStar", "P25"]:
        if mode in o:
            d = o[mode]
            action = d.get("action", "")
            if action in ("start", "late_entry"):
                is_dstar = mode in ("D-Star", "DStar")
                # визначаємо позивний
                if mode == "NXDN":
                    who = d.get("src_callsign") or NXDN_IDS.get(str(d.get("src_id")), d.get("src_info") or d.get("src_id"))
                elif mode == "DMR":
                    who = d.get("src_callsign") or DMR_IDS.get(str(d.get("src_id")), d.get("src_info") or d.get("src_id"))
                else:
                    who = d.get("src_callsign") or d.get("src_info") or d.get("src_id")
                who = who or "?"
                # ціль
                dst = ""
                if is_dstar:
                    tgt = d.get("reflector") or d.get("dst_callsign") or ""
                    if tgt and tgt != "CQCQCQ":
                        dst = tgt.strip()
                elif mode == "YSF":
                    tgt = d.get("reflector") or ""
                    if tgt.strip():
                        dst = tgt.strip()
                else:
                    if d.get("dst_id") is not None:
                        dst = "TG " + str(d.get("dst_id"))
                src = "з радіо" if d.get("source") == "rf" else "з мережі"
                publish_state(client, {
                    "active": True,
                    "mode": "D-Star" if is_dstar else mode,
                    "who": str(who),
                    "dst": dst,
                    "src": src,
                    "ts": time.time()
                })
            elif action == "end":
                publish_state(client, {"active": False})
            return

def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(BROKER, PORT, 60)
            client.loop_forever()
        except Exception as e:
            print("reconnect after error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
