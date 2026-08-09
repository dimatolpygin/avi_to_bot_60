# CI/CD SBAvito

Пайплайн — `.github/workflows/deploy.yml`. Две джобы:

- **test** — на любой push и pull request: ставит пакет и гоняет `pytest`.
  БД/Redis не нужны (тесты на фейковых сессиях и подменённом ИИ-клиенте).
- **deploy** — только когда push в `master`: заходит на сервер по SSH,
  `git pull` + пересобирает прод-стек. На других ветках джоба пропускается
  (правило проекта «автодеплой — только master»).

Репозиторий CI/CD: **github.com/dimatolpygin/sb_group** (публичный). Workflow
репо-агностичен — живёт рядом с кодом; активируется, как только код запушен в
репо с включёнными Actions.

## Что настроить один раз

### 1. Секреты репозитория (Settings → Secrets and variables → Actions)
Ни один из них НЕ хранится в git — только в настройках репо:

| Секрет            | Значение                                                    |
|-------------------|-------------------------------------------------------------|
| `DEPLOY_HOST`     | `45.88.14.140`                                              |
| `DEPLOY_USER`     | `root` (или отдельный deploy-пользователь)                  |
| `DEPLOY_PORT`     | `22`                                                        |
| `DEPLOY_SSH_KEY`  | приватный ключ `доступы/sbavito_deploy` (весь файл целиком) |

Публичная часть ключа (`sbavito_deploy.pub`) уже лежит в `~/.ssh/authorized_keys`
сервера (см. `доступы/server.md`).

### 2. Сервер как git-checkout
Деплой-джоба делает `git pull`, поэтому `/opt/sbavito` должен быть клоном репо
(сейчас там scp-бандл — конвертировать один раз):
```bash
cd /opt/sbavito
git init && git remote add origin https://github.com/dimatolpygin/sb_group.git
git fetch origin && git checkout -f master
```
`.env`, `материалы/`, `secrets/`, `доступы/` — вне git, при `git pull` не трогаются
(в `.gitignore`). БД/данные в docker volume — переживают пересборку.

### 3. Права на push в репо (чтобы код туда попал)
Публичный репо даёт только чтение. Для push нужен write-доступ: добавить
deploy-ключ `sbavito_deploy.pub` как **Deploy key с Allow write access**, либо
использовать PAT. Без этого код в sb_group не зальётся и Actions не запустятся.

## Поток
1. Разработка в `dev`, push в `dev` → бежит только **test**.
2. Мерж `dev → master` по явному указанию → **test** + **deploy** на сервер.
3. Секреты боевые (Авито `client_secret`, amoCRM токен, root-пароль) — перевыпустить
   перед боем (см. `docs/DEPLOY.md`, «Гигиена секретов»).
