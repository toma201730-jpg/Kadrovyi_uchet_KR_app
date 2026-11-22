# Развёртывание на сервере

## Что нужно
- Linux-сервер (Ubuntu 22.04/24.04), 1 ядро и 1–2 ГБ памяти достаточно для 5–200 сотрудников.
- Домен, указывающий на IP сервера; открыты порты 80 и 443 (и 22 для SSH).
- Вариант А (рекомендуется): Docker с плагином compose. Вариант Б: обычный сервер с systemd и nginx.

## Вариант А. Docker (HTTPS получается автоматически)
    unzip kadry_server_package.zip && cd kadry
    cp .env.example .env
    nano .env            # DOMAIN, SECRET_KEY, ADMIN_PASSWORD, при необходимости почта и TZ
    docker compose up -d --build
Откройте `https://ВАШ-ДОМЕН`, войдите как `ADMIN_LOGIN`, **сразу смените пароль** (имя в верхнем меню → профиль)
и удалите `ADMIN_PASSWORD` из `.env`, затем `docker compose up -d`.

Что запускается: контейнер `app` (веб-сервер gunicorn + ежедневные задачи) и `caddy` (HTTPS-сертификат и прокси).
Данные лежат в томе `kadry_data` (`/data`: база, сканы, резервные копии) и сохраняются при перезапуске и обновлении.
Логи: `docker compose logs -f app`. Проверка состояния: `docker compose ps` (статус healthy).

Первый запуск без домена, для пробы на своём компьютере: `DOMAIN=localhost` в `.env`.

### Ежедневные задачи (работают сами, отдельного cron не нужно)
- 02:00 (по `TZ`) — резервная копия в `/data/backups`, хранится `BACKUP_KEEP` последних (по умолчанию 14).
- 08:00 — проверка сроков и рассылка напоминаний кадровику (нужна настройка почты `SMTP_*`).
Если сервер был выключен в это время, задача выполнится при старте. Результаты видны в `docker compose logs app`.

### Копии за пределы сервера (обязательно)
Копия на том же диске не защитит от потери сервера. Два способа:
1. **Команда выгрузки.** `BACKUP_UPLOAD_CMD` в `.env` — команда, которой путь новой копии передаётся последним
   аргументом. Она должна быть доступна внутри контейнера (в образ rclone и подобное не входят: добавьте в Dockerfile
   или используйте свой скрипт). Сбой выгрузки пишется в журнал действий (`backup_upload_failed`) и в лог.
2. **Копирование с хоста по cron** (шаблон, на реальном сервере не проверялся):
       0 3 * * *  cd /opt/kadry && docker compose cp app:/data/backups/. /srv/kadry-backups/ && rclone copy /srv/kadry-backups remote:kadry
Раз в квартал проверяйте, что копию действительно можно восстановить (ниже).

### Восстановление из копии
    docker compose stop app
    docker compose run --rm --no-deps --entrypoint "flask --app app restore /data/backups/kadry-backup-ИМЯ.zip --force" app
    docker compose up -d
Если копия лежит вне тома, сначала положите её в `/data/backups` (`docker compose cp файл.zip app:/data/backups/`
выполняется при запущенном контейнере). После восстановления войдите и проверьте список сотрудников и журнал.

### Обновление версии
Распакуйте новую версию поверх, сохранив `.env`, затем `docker compose up -d --build`. Схема базы обновляется сама
(`init-db` безопасно запускать повторно, данные сохраняются). Перед обновлением сделайте копию:
`docker compose exec app flask --app app backup --out /data/backups`.

## Вариант Б. Сервер без Docker (systemd + nginx)
    sudo apt install python3-venv nginx certbot python3-certbot-nginx
    sudo useradd --system --create-home --home-dir /opt/kadry kadry
    sudo -u kadry mkdir -p /opt/kadry/app /opt/kadry/data && sudo -u kadry unzip kadry_server_package.zip -d /opt/kadry/app
    sudo -u kadry python3 -m venv /opt/kadry/venv && sudo -u kadry /opt/kadry/venv/bin/pip install -r /opt/kadry/app/requirements.txt
Создайте `/etc/kadry.env` (права `chmod 600`, владелец root) со строками из `.env.example`, плюс:
    APP_ENV=production
    TRUST_PROXY=1
    DATABASE=/opt/kadry/data/kadry.db
    UPLOAD_DIR=/opt/kadry/data/uploads
    BACKUP_DIR=/opt/kadry/data/backups
Дальше:
    sudo cp deploy/kadry.service deploy/kadry-scheduler.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now kadry kadry-scheduler
    sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/kadry   # заменить домен
    sudo ln -s /etc/nginx/sites-available/kadry /etc/nginx/sites-enabled/ && sudo certbot --nginx -d ВАШ-ДОМЕН
Восстановление: `sudo systemctl stop kadry kadry-scheduler`, затем
`sudo -u kadry env $(cat /etc/kadry.env | xargs) /opt/kadry/venv/bin/flask --app app restore ФАЙЛ.zip --force`, затем запуск служб.

## Безопасность сервера (минимум)
- Только HTTPS; `SECRET_KEY` длинный и секретный; `.env` и `/etc/kadry.env` недоступны другим пользователям.
- Брандмауэр: открыть только 22, 80, 443 (`ufw allow 22,80,443/tcp && ufw enable`); вход по SSH-ключам.
- Регулярные обновления ОС и образов (`docker compose pull && docker compose up -d --build`).
- Копии вне сервера; доступ к ним только у ответственных. В копиях персональные данные сотрудников.

## Что проверено, а что нет (честно)
Проверено в среде разработки: приложение, тесты (в том числе планировщик, копии и восстановление), последовательность
запуска контейнера (инициализация базы, создание администратора, ежедневные задачи, вход), синтаксис compose и shell,
unit-файлы systemd.
**Не проверено:** сборка Docker-образа, запуск gunicorn, Caddy и nginx, получение сертификата, отправка настоящей
почты: в среде разработки нет Docker, доступа в интернет и почтового сервера. Эти шаги нужно выполнить и проверить
на сервере; возможные правки будут небольшими (версии пакетов, права на файлы).

## Чего в этой версии нет
- PostgreSQL: код написан под SQLite, приложение должно работать на одном сервере. Перенос на PostgreSQL — отдельная
  задача: типы и `AUTOINCREMENT` в `schema.sql` (13 мест), `datetime('now')`/`date('now')` (28), `INSERT OR IGNORE` (3),
  `PRAGMA` (5), `COLLATE NOCASE` (2), индексы по выражению `substr(...)` (2), `executescript`, плейсхолдеры `?`.
- Облачное объектное хранилище для сканов: файлы лежат в `UPLOAD_DIR` (`/data/uploads`).
