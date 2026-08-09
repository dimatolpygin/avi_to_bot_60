# Деплой SBAvito на сервер (этап 14.5)

Боевой сервер: **45.88.14.140** (Ubuntu 24.04), домен `bot-admin.online`.
Код и стек живут в `/opt/sbavito`, запускаются через `docker-compose.prod.yml`.

Этап разбит на два шага (сцепка вебхук↔Jivo, см. `07_ROADMAP.md`):
- **Шаг 1 (сделан) — поллинг на сервере, белый список, Jivo не трогаем.** Наружу
  ничего не открыто (ни вебхука, ни nginx): и поллинг Авито, и amoCRM — только
  исходящие. Безопасно поднять рядом с живым Jivo.
- **Шаг 2 (позже) — вебхук вместо поллинга + «отвечаем всем».** Требует публичного
  HTTPS на `bot-admin.online` (nginx+TLS) и разводки с Jivo: подписка вебхука на
  аккаунте Авито ОДНА, наша вытеснит Jivo. Раздел ниже — только эскиз.

---

## Доступ к серверу

- Вход по SSH-ключу (пароль root засветился в переписке — **сменить**). Deploy-ключ
  сгенерирован при выкладке; хранить вне git (см. `доступы/server.md`).
- На сервере установлены Docker + compose plugin (`curl -fsSL https://get.docker.com | sh`).

```bash
ssh -i <deploy-key> root@45.88.14.140
```

---

## Шаг 1 — выкладка (как это делалось и как повторить)

Compose самодостаточен: поднимает СВОИ `postgres:16` и `redis:7` (сервер наш и
пустой — в отличие от dev-хоста, где БД/Redis чужие). Код печётся в образ, без
watchmedo и без монтирования кода — «что в образе, то и работает».

### 1. Файлы на сервер (в `/opt/sbavito`)
Из git приезжает всё, КРОМЕ игнорируемого. Отдельно кладём (scp), потому что они
вне git:
- `.env` — секреты (собрать из `.env.example`, значения из `доступы/`);
- `материалы/прайс/*.csv` — прайс для разового `import_prays`;
- `secrets/service-account.json` — ключ Google (только если включаешь синк; в шаге 1
  синк ВЫКЛЮЧЕН: `GOOGLE_CREDS_PUT=` пустой).

`.env` шага 1 отличается от dev тремя ключами:
```
AVITO_REZHIM=spisok
AVITO_BELYY_SPISOK=u2i-kjcXAtwAdcHYfcX6085xjw   # тестовый чат; на бою → vse (шаг 2)
AMO_ZERKALO=on
GOOGLE_CREDS_PUT=                                # синк выключен (переход — отд. решение)
```
`PGHOST`/`REDIS_URL` в `.env` не важны — `docker-compose.prod.yml` переопределяет
их на имена сервисов (`postgres`/`redis`).

### 2. Подъём стека
```bash
cd /opt/sbavito
docker compose -f docker-compose.prod.yml up -d --build
```
`entrypoint.sh` сам гонит `alembic upgrade head` перед стартом. `app` ждёт, пока
`postgres` не станет healthy.

### 3. Наполнение БД (свежая база пустая)
```bash
C='docker compose -f docker-compose.prod.yml exec -T app'
$C python -m bot.seed             # аккаунты + базовый промпт
$C python -m bot.etl.import_prays # прайс Saunamart → products (141 позиция)
$C python -m bot.seed_znaniya     # база знаний услуг (блоки)
docker compose -f docker-compose.prod.yml restart app   # подхватить каталог/знания ИЗ БД
```

### 4. Проверка
```bash
docker compose -f docker-compose.prod.yml exec -T app python -m bot.proverka  # схема/соединения/rid
docker compose -f docker-compose.prod.yml logs app --tail=40                  # старт ботов
docker compose -f docker-compose.prod.yml ps                                  # все healthy
```
В логе должно быть: «Каталог загружен: … (БД, аккаунт saunamart)», «промпт услуг
собран из базы знаний БД», «Авито «sbsauna» … белый список: u2i-…, зеркало в
amoCRM», три бота подняты.

**Живой критерий шага 1**: из тестового чата Авито `u2i-kjcX…` приходит сообщение →
в логе ответ бота с сервера и `🪞 → amoCRM`; чужие чаты в логе идут как `🔇 … не в
белом списке — пропускаю` (живые клиенты защищены, Jivo отвечает им как раньше).

---

## Обновление кода (redeploy)
Код печётся в образ, поэтому нужна пересборка:
```bash
# новый код в /opt/sbavito (scp/git pull), затем:
cd /opt/sbavito && docker compose -f docker-compose.prod.yml up -d --build
```
Правка только `.env` → `docker compose -f docker-compose.prod.yml up -d --force-recreate app`.

## Откат
```bash
cd /opt/sbavito && docker compose -f docker-compose.prod.yml down   # стоп (данные в volume целы)
# полный снос с данными: добавить -v (снесёт БД!)
```
Локальный dev-стек — рабочий резерв; при остановке сервера можно вернуть локально.

⚠️ **Один поллер на токен.** Сервер теперь авторитетный: локальный контейнер и любой
`python -m bot.main` должны быть выключены, иначе `TelegramConflictError` на TG-токенах
и дубли ответов в белом чате Авито.

---

## Гигиена секретов (шаг 1 → бой)
- В публичном репо не должно быть ни одного секрета; `.env`, `доступы/`, `secrets/`,
  `материалы/` — в `.gitignore`.
- **Перевыпустить перед боем** (пришли перепиской): Авито `client_secret`,
  amoCRM `AMO_ACCESS_TOKEN`, root-пароль сервера. Желательно и OpenRouter-ключ.
- Обновил секрет → правишь `.env` на сервере → `up -d --force-recreate app`.

---

## Шаг 2 — вебхук + «отвечаем всем» (эскиз, ещё не делалось)
1. DNS `bot-admin.online` → 45.88.14.140; nginx + certbot (TLS), проксирует на
   контейнер (нужно добавить в приложение HTTP-приёмник вебхука Авито).
2. Развести Jivo: на аккаунте Авито подписка вебхука одна — либо Jivo отключают,
   либо мы регистрируем свою поверх (Jivo перестанет получать). Согласовать ДО.
3. Зарегистрировать вебхук Авито на входящие, снять поллинг.
4. `.env`: `AVITO_REZHIM=vse` (белый список снимается). Проверить, что нет дублей.
5. Тот же публичный HTTPS нужен виджет-панели amoCRM (этап 14.7) и приёму ответа
   менеджера из amo клиенту в Авито (хвост 14.3b).
