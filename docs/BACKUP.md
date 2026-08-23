# Резервне копіювання

Два рівні бекапу, які доповнюють один одного:

| Тип | Що містить | Розмір | Час | Як часто |
|---|---|---|---|---|
| **Архів конфігів** | усі `.ini`, дашборд, юніти, ключі | ~50 КБ | секунди | після кожної зміни |
| **Повний образ SD** | вся система посекторно | ~1.7 ГБ | 1–1.5 год | раз на тиждень-два |

Архів конфігів дозволяє швидко відновити налаштування на свіжій системі.
Повний образ — це відновлення «як було» одним записом на картку.

---

## 1. Налаштування rclone (Google Drive)

Один раз на Pi:

```bash
sudo apt install -y rclone
rclone config
```

Створіть remote з назвою `gdrive` типу *Google Drive*. Перевірка:

```bash
rclone lsd gdrive:
```

> Виконуйте `rclone` **від користувача `pi`**, а не через `sudo` — конфіг
> лежить у `~/.config/rclone/rclone.conf` і під root не знайдеться.

---

## 2. Архів конфігів (скрипт)

```bash
sudo cp scripts/mmdvm-backup /usr/local/bin/
sudo chmod +x /usr/local/bin/mmdvm-backup
```

Відредагуйте змінну `REMOTE` у скрипті під свій шлях на Drive.

Запуск:

```bash
sudo mmdvm-backup
```

Скрипт збирає конфіги всіх сервісів, файли дашборду, systemd-юніти, cron
і CLI-помічники, пакує в `config-backup-YYYYMMDD-HHMM.tar.gz` і вивантажує
на Drive. Відсутні файли пропускаються з поміткою `! немає:` — це нормально,
якщо якийсь компонент у вас не встановлено.

### Автоматично раз на добу

```bash
echo '0 4 * * * root /usr/local/bin/mmdvm-backup >/dev/null 2>&1' | sudo tee /etc/cron.d/mmdvm-backup
```

### Відновлення з архіву

```bash
sudo tar xzf config-backup-YYYYMMDD-HHMM.tar.gz -C /
sudo systemctl daemon-reload
sudo systemctl restart mmdvmhost dmrgateway dashboard
```

---

## 3. Повний образ SD-карти

Знімається **з живої системи** прямо на Pi, стрімом на Google Drive — без
проміжного файлу на самій картці (місця на неї не потрібно).

```bash
screen -dmS imgbackup bash -c 'sudo dd if=/dev/mmcblk0 bs=4M | gzip | rclone rcat gdrive:Backup/PiBackup/pi-backup-$(date +%Y%m%d).img.gz; touch /home/pi/BACKUP_DONE'
```

Розбір ланцюга:
- `dd` читає **весь диск** — обидва розділи, таблицю розділів, завантажувач
- `gzip` стискає на льоту (порожнеча картки стискається дуже добре)
- `rclone rcat` пише потік одразу в хмару
- `screen -dmS` тримає процес, навіть якщо SSH-сесія обірветься

Перевірка, що пішло:

```bash
ps aux | grep -E "dd if=/dev/mmcblk0|rclone rcat" | grep -v grep
```

Перевірка завершення:

```bash
ls -la /home/pi/BACKUP_DONE 2>/dev/null && echo "ГОТОВО" || echo "ще йде"
```

Після завершення прибрати сесію:

```bash
screen -S imgbackup -X quit
```

> **Увага:** образ знімається з працюючої системи, тому теоретично можлива
> неконсистентність відкритих файлів. На практиці для хотспота це не критично
> (бази даних немає), але найнадійніший образ — знятий з вимкненого Pi через
> картрідер на іншому комп'ютері.

### Чому образ такий великий при відновленні

`dd if=/dev/mmcblk0` копіює карту цілком, включно з незайнятим місцем.
Образ з карти 64 ГБ вимагатиме при відновленні карту **не меншу за 64 ГБ**,
навіть якщо реально зайнято 5 ГБ. Стиснутий `.img.gz` при цьому важить
~1.7 ГБ, бо нулі добре стискаються.

---

## 4. Стиснення образу через PiShrink

Щоб образ можна було записати на **меншу** картку, проженіть його через
[PiShrink](https://github.com/Drewsif/PiShrink). PiShrink ужимає ext4-розділ
до мінімуму і додає скрипт авторозширення — при першому завантаженні Pi сам
розтягне файлову систему на весь об'єм нової картки.

Робиться на комп'ютері (Linux або Windows + WSL2), не на Pi.

### Підготовка WSL2 (один раз)

```bash
sudo apt update
sudo apt install -y parted e2fsprogs gzip wget
wget https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
sudo mv pishrink.sh /usr/local/bin/pishrink
sudo chmod +x /usr/local/bin/pishrink
```

### Обробка образу

```bash
cd /mnt/d
gunzip -c pi-backup-YYYYMMDD.img.gz > pi-backup-YYYYMMDD.img
sudo pishrink -z pi-backup-YYYYMMDD.img pi-backup-YYYYMMDD-shrunk.img
```

Результат — `pi-backup-YYYYMMDD-shrunk.img.gz`, який пишеться на картку від
8–16 ГБ через Raspberry Pi Imager (*Use custom image*) або balenaEtcher.

### Особливості WSL

- `/mnt/d` — це NTFS через WSL, тому `gunzip -k file.gz` падає з
  `Operation not permitted`. Обхід — перенаправлення потоку:
  `gunzip -c file.gz > file.img`
- Нове вікно Ubuntu стартує в `~`, а не на диску D. Завжди спочатку
  `cd /mnt/d`, інакше отримаєте `No such file or directory`
- Тримайте образи на `/mnt/d`, а не в домашній теці WSL — там більше місця

---

## 5. Що ще варто зберігати

- **Конфіг rclone** (`~/.config/rclone/rclone.conf`) — містить токен доступу
  до Drive. Якщо втратите разом із карткою, доведеться авторизуватись наново
- **`admin_pass.hash`** — хеш пароля дашборду (в архів конфігів входить)
- **`bm_apikey`** — ключ BrandMeister API (в архів конфігів входить)

> Ці три файли містять секрети. Ніколи не додавайте їх у git.
