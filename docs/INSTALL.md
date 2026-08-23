# Інструкція зі встановлення

Ця інструкція припускає, що у вас уже є робочий хотспот MMDVM (MMDVMHost та
відповідні шлюзи/рефлектори) на Raspberry Pi з Debian.

Якщо щось не запрацювало — дивіться [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. Панель (Dashboard)

```bash
sudo mkdir -p /opt/dashboard
sudo cp dashboard/dashboard.py /opt/dashboard/
sudo cp dashboard/itu_flags.py /opt/dashboard/
pip3 install flask paho-mqtt --break-system-packages
```

> `itu_flags.py` відповідає за прапорці країн у стрічці активності для
> YSF / NXDN / D-Star. Без нього панель працює, але прапорці будуть лише
> для DMR (за MCC-префіксом DMR ID).

Запуск як systemd-сервіс (створіть `/etc/systemd/system/dashboard.service`):

```ini
[Unit]
Description=MMDVM Dashboard
After=network.target mosquitto.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/dashboard/dashboard.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard
```

Відкрийте `http://<ip-вашого-pi>:85`.

### ВАЖЛИВО: змініть пароль адміністратора

Типовий пароль адміністратора — **`passw0rd`**. Увійдіть через кнопку
**Адмін** і одразу змініть його. Хеш пароля зберігається у файлі
`/opt/dashboard/admin_pass.hash` (цей файл ніколи не можна додавати в git).

## 2. CLI-помічники для оновлень

```bash
sudo cp scripts/check-updates scripts/update-service /usr/local/bin/
sudo chmod +x /usr/local/bin/check-updates /usr/local/bin/update-service
```

- `check-updates` — лише читання, показує, які git-сервіси відстають
- `sudo update-service <назва>` — оновлює один сервіс з автоматичним бекапом і
  відкатом, якщо збірка чи перезапуск не вдалися

Підтримувані сервіси: `mmdvmhost`, `dmrgateway`, `ysfgateway`, `nxdngateway`.

## 3. DMR через DMRGateway (обов'язково для DMR)

Сучасний MMDVMHost не може підключитися до DMR-майстра напряму. Встановіть
DMRGateway:

```bash
cd /opt
sudo git clone https://github.com/g4klx/DMRGateway.git
cd DMRGateway
sudo apt install -y libmosquitto-dev
sudo make
```

Скопіюйте й відредагуйте приклад конфігу:

```bash
sudo cp /шлях/до/config-samples/DMRGateway.ini.sample /opt/DMRGateway/DMRGateway.ini
sudo nano /opt/DMRGateway/DMRGateway.ini
```

Заповніть заповнювачі:
- `YOUR_DMR_ID` — ваш DMR ID
- `YOUR_CALLSIGN` — ваш позивний
- `YOUR_RX_FREQ` / `YOUR_TX_FREQ` — частоти в Гц (напр. `438100000`)
- У `[DMR Network 1]`: `YOUR_MASTER_IP`, `YOUR_MASTER_PORT`,
  `YOUR_MASTER_PASSWORD` — ваш DMR-майстер (HBLink / Brandmeister / TGIF)

Спрямуйте MMDVMHost на локальний DMRGateway. У `/opt/MMDVMHost/MMDVM-Host.ini`,
секція `[DMR Network]`:

```ini
GatewayAddress=127.0.0.1
GatewayPort=62031
```

Встановіть systemd-юніт:

```bash
sudo cp systemd/dmrgateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dmrgateway
sudo systemctl restart mmdvmhost
```

Перевірте на боці DMR-майстра, що ваш хотспот логіниться (ви маєте побачити
обмін `RPTL` / логін і періодичні `RPTPING`, а не "Unrecognized command").

> Для **симплексних** хотспотів у секції `[Info]` обов'язково має бути
> `Duplex=0`, інакше BrandMeister відхилить підключення. Подробиці —
> у [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 4. Прибирання логів (необов'язково)

```bash
sudo cp config-samples/mmdvm-cleanup.cron /etc/cron.d/mmdvm-cleanup
```

Це видаляє логи MMDVM старші 7 днів і ротує логи Apache та бекапи.

## 5. Логи для дашборду (journal2log)

Дашборд читає добові файли логів із `/var/log/mmdvm/`:

```
/var/log/mmdvm/MMDVM-YYYY-MM-DD.log
/var/log/mmdvm/NXDNGateway-YYYY-MM-DD.log
```

Але якщо MMDVMHost і шлюзи запускаються через systemd, вони часто пишуть
**у journal**, а не у файли — і тоді таблиця активності буде порожньою.
Міст `journal2log` перенаправляє journal у потрібні файли:

```bash
sudo cp scripts/journal2log /usr/local/bin/
sudo chmod +x /usr/local/bin/journal2log
sudo mkdir -p /var/log/mmdvm

sudo cp systemd/mmdvm-journal2log.service /etc/systemd/system/
sudo cp systemd/nxdn-journal2log.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mmdvm-journal2log nxdn-journal2log
```

Перевірка (файл має з'явитись і рости під час передач):

```bash
ls -la /var/log/mmdvm/MMDVM-$(date +%F).log
```

Скрипт універсальний — для будь-якого іншого сервіса:

```bash
journal2log <systemd-юніт> <префікс-файлу> [тека]
```

> Якщо ваш MMDVMHost уже пише у файл напряму (параметр `FilePath` у секції
> `[Log]` конфігу), цей крок не потрібен.

## 6. D-Star (ircddbgateway)

Дашборд показує статус D-Star-лінка, читаючи логи ircddbgateway
(`/var/log/ircddbgateway/ircDDBGateway-DATE.log`) і конфіг `/etc/ircddbgateway`.

Автоматичний лінк до рефлектора при старті:

```ini
atStartup1=1
reflector1=XLX257 A
```

> Відомий нюанс: лінк інколи відпадає по `poll inactivity`, і автоматичного
> релінку не відбувається. Лікується перезапуском ircddbgateway — див.
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 7. Резервне копіювання

Схема бекапів (архів конфігів + повний образ SD-карти, вивантаження на
Google Drive, стиснення через PiShrink) описана окремо:
**[BACKUP.md](BACKUP.md)**.

Мінімум, який варто зробити одразу після встановлення:

```bash
sudo cp scripts/mmdvm-backup /usr/local/bin/
sudo chmod +x /usr/local/bin/mmdvm-backup
sudo mmdvm-backup
```

## Примітки щодо інших конфігів

Конфіги MMDVMHost, YSFGateway, NXDNGateway, рефлекторів і DVSwitch — це
стандартні файли з проєктів [g4klx](https://github.com/g4klx) і
[DVSwitch](https://github.com/DVSwitch) — налаштовуйте їх за тими проєктами.
Тут наведено лише приклад DMRGateway, бо саме він є ключовою ланкою для
прямого підключення DMR до майстра.

### Нюанс конфігу MMDVMHost (новіші версії)

Новіші версії MMDVMHost перенесли ключі частот RX/TX із секції `[Info]` у
`[Modem]`. Якщо DMR/модем повідомляє `0 Hz` і ви отримуєте
`Received a NAK to the SET_FREQ command`, додайте в `[Modem]`:
```ini
RXFrequency=YOUR_RX_FREQ
TXFrequency=YOUR_TX_FREQ
```

### YSFGateway

Щоб шлюз повертався на домашню кімнату після роботи в іншій:

```ini
Revert=1
```
