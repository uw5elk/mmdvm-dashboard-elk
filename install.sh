#!/bin/bash
#
# MMDVM Dashboard ELK - інсталяційний скрипт
# Встановлює багаторежимний MMDVM хотспот-стек + дашборд на Raspberry Pi (Debian).
#
# Використання:  sudo ./install.sh
#
# УВАГА: скрипт встановлює й компілює багато компонентів. Перед запуском на
# робочій системі зробіть бекап. Рекомендовано запускати на чистій системі.
#
set -u

# --- Кольори ---
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
say()  { echo -e "${G}>>>${N} $*"; }
warn() { echo -e "${Y}!!!${N} $*"; }
err()  { echo -e "${R}ПОМИЛКА:${N} $*" >&2; }
ask()  { local p="$1" d="${2:-}" v; read -rp "$(echo -e "${B}?${N} $p ${d:+[$d] }")" v < /dev/tty; echo "${v:-$d}"; }
yn()   { local a; printf "${B}?${N} %s (y/n) [%s] " "$1" "${2:-n}" > /dev/tty; read -r a < /dev/tty; a="${a:-${2:-n}}"; [[ "$a" =~ ^[Yy] ]]; }

# --- Перевірки ---
if [ "$(id -u)" -ne 0 ]; then err "Запустіть через sudo."; exit 1; fi
if ! grep -qiE "debian|raspbian" /etc/os-release 2>/dev/null; then
    warn "Систему не розпізнано як Debian/Raspberry Pi OS. Продовження може бути ризикованим."
    yn "Продовжити попри це?" n || exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
OPT=/opt
LOGDIR=/var/log/mmdvm

echo -e "${G}"
echo "=================================================="
echo "  MMDVM Dashboard ELK - встановлення"
echo "=================================================="
echo -e "${N}"
echo "Скрипт встановить обрані компоненти MMDVM хотспота."
echo "Базовий стек (MMDVMHost + модем) можна встановити тут або"
echo "використати наявний (Pi-Star / WPSD / ручне встановлення)."
echo

# ============================================================
# КРОК 1: Параметри станції
# ============================================================
say "Крок 1: Параметри вашої станції"
CALLSIGN=$(ask "Ваш позивний (callsign)" "N0CALL")
DMR_ID=$(ask "Ваш DMR ID" "0000000")
RX_FREQ=$(ask "RX частота в Гц (напр. 438100000)" "438100000")
TX_FREQ=$(ask "TX частота в Гц (напр. 438100000)" "$RX_FREQ")
echo

# ============================================================
# КРОК 2: Вибір компонентів
# ============================================================
say "Крок 2: Що встановити? (Enter = типове значення)"
DO_DEPS=1
INST_MMDVMHOST=$(yn "Встановити MMDVMHost (базовий, потрібен модем)?" n && echo 1 || echo 0)
INST_DMRGW=$(yn "Встановити DMRGateway (для DMR через майстер)?" y && echo 1 || echo 0)
INST_YSF=$(yn "Встановити YSFGateway + YSFReflector?" y && echo 1 || echo 0)
INST_NXDN=$(yn "Встановити NXDNGateway + NXDNReflector?" y && echo 1 || echo 0)
INST_DASH=$(yn "Встановити дашборд?" y && echo 1 || echo 0)
INST_CLI=$(yn "Встановити CLI-скрипти (check-updates, update-service)?" y && echo 1 || echo 0)
INST_CRON=$(yn "Встановити cron-очищення логів?" y && echo 1 || echo 0)
echo

# Дані DMR-майстра (лише якщо ставимо DMRGateway)
if [ "$INST_DMRGW" = "1" ]; then
    say "Дані вашого DMR-майстра (HBLink / Brandmeister / TGIF)"
    MASTER_NAME=$(ask "Назва мережі (напр. HBLink)" "HBLink")
    MASTER_IP=$(ask "IP/хост майстра" "127.0.0.1")
    MASTER_PORT=$(ask "Порт майстра" "55555")
    MASTER_PASS=$(ask "Пароль майстра" "passw0rd")
    echo
fi

# Підсумок
say "Підсумок:"
echo "  Позивний: $CALLSIGN | DMR ID: $DMR_ID | RX: $RX_FREQ | TX: $TX_FREQ"
echo "  MMDVMHost=$INST_MMDVMHOST DMRGateway=$INST_DMRGW YSF=$INST_YSF NXDN=$INST_NXDN"
echo "  Дашборд=$INST_DASH CLI=$INST_CLI Cron=$INST_CRON"
echo
yn "Продовжити встановлення?" y || { echo "Скасовано."; exit 0; }
echo

mkdir -p "$LOGDIR"

# ============================================================
# Функція: клон + компіляція git-проєкту
# ============================================================
build_git() {
    local url="$1" dir="$2" builddir="${3:-$2}"
    if [ -d "$dir/.git" ]; then
        warn "$dir вже існує - пропускаю клон"
    else
        say "Клоную $url -> $dir"
        git clone --depth 1 "$url" "$dir" || { err "клон $url не вдався"; return 1; }
    fi
    say "Компілюю $builddir ..."
    make -C "$builddir" || { err "збірка $builddir не вдалася"; return 1; }
}

# systemd-юніт-помічник
make_unit() {
    local name="$1" desc="$2" exec="$3" after="${4:-network.target}"
    cat > "/etc/systemd/system/$name.service" << UNIT
[Unit]
Description=$desc
After=$after

[Service]
Type=simple
ExecStart=$exec
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable "$name" >/dev/null 2>&1
}

# ============================================================
# КРОК 3: Залежності
# ============================================================
if [ "$DO_DEPS" = "1" ]; then
    say "Крок 3: Встановлення залежностей (apt)..."
    apt-get update
    apt-get install -y git build-essential make g++ \
        python3 python3-pip python3-flask python3-paho-mqtt \
        mosquitto mosquitto-clients \
        libmosquitto-dev \
        parted e2fsprogs \
        2>&1 | tail -5
    # paho-mqtt через pip, якщо apt-пакет відсутній
    python3 -c "import paho.mqtt" 2>/dev/null || pip3 install paho-mqtt --break-system-packages 2>/dev/null
    python3 -c "import flask" 2>/dev/null || pip3 install flask --break-system-packages 2>/dev/null
    systemctl enable --now mosquitto 2>/dev/null
    say "Залежності встановлено."
fi

# ============================================================
# КРОК 4: MMDVMHost (опціонально)
# ============================================================
if [ "$INST_MMDVMHOST" = "1" ]; then
    say "Крок 4: MMDVMHost"
    build_git "https://github.com/g4klx/MMDVMHost.git" "$OPT/MMDVMHost" && {
        make_unit "mmdvmhost" "MMDVM Host" "$OPT/MMDVMHost/MMDVMHost $OPT/MMDVMHost/MMDVM-Host.ini"
        warn "MMDVMHost встановлено, але потребує ручного налаштування MMDVM-Host.ini"
        warn "(модем, частоти в [Modem], позивний, DMR ID). Приклад - у документації g4klx."
    }
fi

# ============================================================
# КРОК 5: DMRGateway
# ============================================================
if [ "$INST_DMRGW" = "1" ]; then
    say "Крок 5: DMRGateway"
    build_git "https://github.com/g4klx/DMRGateway.git" "$OPT/DMRGateway" && {
        # конфіг з шаблону
        if [ -f "$REPO_DIR/config-samples/DMRGateway.ini.sample" ]; then
            cp "$REPO_DIR/config-samples/DMRGateway.ini.sample" "$OPT/DMRGateway/DMRGateway.ini"
        fi
        # підстановка параметрів
        sed -i \
            -e "s|YOUR_DMR_ID|$DMR_ID|g" \
            -e "s|YOUR_CALLSIGN|$CALLSIGN|g" \
            -e "s|YOUR_RX_FREQ|$RX_FREQ|g" \
            -e "s|YOUR_TX_FREQ|$TX_FREQ|g" \
            -e "s|YOUR_MASTER_IP|$MASTER_IP|g" \
            -e "s|YOUR_MASTER_PORT|$MASTER_PORT|g" \
            -e "s|YOUR_MASTER_PASSWORD|$MASTER_PASS|g" \
            "$OPT/DMRGateway/DMRGateway.ini"
        sed -i "s|^Name=HBLink|Name=$MASTER_NAME|" "$OPT/DMRGateway/DMRGateway.ini"
        make_unit "dmrgateway" "DMR Gateway" "$OPT/DMRGateway/DMRGateway $OPT/DMRGateway/DMRGateway.ini" "mmdvmhost.service"
        say "DMRGateway налаштовано на майстер $MASTER_NAME ($MASTER_IP:$MASTER_PORT)"
        warn "У MMDVMHost [DMR Network] вкажіть: GatewayAddress=127.0.0.1, GatewayPort=62031"
    }
fi

# ============================================================
# КРОК 6: YSF (Gateway + Reflector)
# ============================================================
if [ "$INST_YSF" = "1" ]; then
    say "Крок 6: YSFGateway + YSFReflector"
    build_git "https://github.com/g4klx/YSFClients.git" "$OPT/YSFClients" "$OPT/YSFClients/YSFGateway" && \
        make -C "$OPT/YSFClients/YSFReflector" 2>/dev/null
    [ -f "$OPT/YSFClients/YSFGateway/YSFGateway" ] && \
        make_unit "ysfgateway" "YSF Gateway" "$OPT/YSFClients/YSFGateway/YSFGateway $OPT/YSFClients/YSFGateway/YSFGateway.ini" "mmdvmhost.service"
    warn "YSFGateway/YSFReflector встановлено - налаштуйте .ini (Startup, Revert=1) вручну."
fi

# ============================================================
# КРОК 7: NXDN (Gateway + Reflector)
# ============================================================
if [ "$INST_NXDN" = "1" ]; then
    say "Крок 7: NXDNGateway + NXDNReflector"
    build_git "https://github.com/g4klx/NXDNClients.git" "$OPT/NXDNClients" "$OPT/NXDNClients/NXDNGateway" && \
        make -C "$OPT/NXDNClients/NXDNReflector" 2>/dev/null
    [ -f "$OPT/NXDNClients/NXDNGateway/NXDNGateway" ] && \
        make_unit "nxdngateway" "NXDN Gateway" "$OPT/NXDNClients/NXDNGateway/NXDNGateway $OPT/NXDNClients/NXDNGateway/NXDNGateway.ini" "mmdvmhost.service"
    warn "NXDNGateway/NXDNReflector встановлено - налаштуйте .ini вручну."
fi

# ============================================================
# КРОК 8: Дашборд
# ============================================================
if [ "$INST_DASH" = "1" ]; then
    say "Крок 8: Дашборд"
    mkdir -p "$OPT/dashboard"
    if [ -f "$REPO_DIR/dashboard/dashboard.py" ]; then
        cp "$REPO_DIR/dashboard/dashboard.py" "$OPT/dashboard/dashboard.py"
        make_unit "dashboard" "MMDVM Dashboard" "/usr/bin/python3 $OPT/dashboard/dashboard.py" "network.target mosquitto.service"
        say "Дашборд встановлено на порту 85."
        warn "Типовий пароль адміністратора: passw0rd - ЗМІНІТЬ його після першого входу!"
    else
        err "dashboard.py не знайдено в $REPO_DIR/dashboard/ - пропускаю."
    fi
fi

# ============================================================
# КРОК 9: CLI-скрипти
# ============================================================
if [ "$INST_CLI" = "1" ]; then
    say "Крок 9: CLI-скрипти"
    for s in check-updates update-service; do
        if [ -f "$REPO_DIR/scripts/$s" ]; then
            cp "$REPO_DIR/scripts/$s" "/usr/local/bin/$s"
            chmod +x "/usr/local/bin/$s"
        fi
    done
    say "CLI-скрипти встановлено (check-updates, update-service)."
fi

# ============================================================
# КРОК 10: Cron-очищення
# ============================================================
if [ "$INST_CRON" = "1" ]; then
    say "Крок 10: Cron-очищення логів"
    if [ -f "$REPO_DIR/config-samples/mmdvm-cleanup.cron" ]; then
        cp "$REPO_DIR/config-samples/mmdvm-cleanup.cron" "/etc/cron.d/mmdvm-cleanup"
        say "Cron-очищення встановлено."
    fi
fi

# ============================================================
# ФІНАЛ: запуск сервісів і підсумок
# ============================================================
echo
say "Запуск встановлених сервісів..."
for svc in mmdvmhost dmrgateway ysfgateway nxdngateway dashboard; do
    if [ -f "/etc/systemd/system/$svc.service" ]; then
        systemctl restart "$svc" 2>/dev/null && echo "  $svc: $(systemctl is-active $svc)"
    fi
done

echo
echo -e "${G}=================================================="
echo "  Встановлення завершено!"
echo -e "==================================================${N}"
echo
say "Що далі:"
echo "  1. Дашборд: http://<ip-цього-pi>:85"
echo "     Пароль адміністратора: passw0rd - ОБОВ'ЯЗКОВО змініть!"
if [ "$INST_MMDVMHOST" = "1" ]; then
    echo "  2. Налаштуйте /opt/MMDVMHost/MMDVM-Host.ini (модем, частоти, позивний)"
    echo "     Частоти RX/TX у новіших версіях - у секції [Modem]!"
fi
if [ "$INST_DMRGW" = "1" ]; then
    echo "  3. У MMDVMHost [DMR Network]: GatewayAddress=127.0.0.1, GatewayPort=62031"
fi
echo "  4. Перевірка статусу: systemctl status <сервіс>"
echo "  5. Логи: journalctl -u <сервіс> -f"
echo
say "73!"
