-- Разовая зачистка: снести аннулированную схему этапа 1 (Drizzle) из public
-- в БД sbavito. Миграцией это не оформлено сознательно — Alembic отвечает за
-- схему `sbavito`, а не за чужую территорию; после разворота стека на Python
-- эти таблицы просто мусор, и повторно они не появятся.
--
-- Запуск:
--   docker exec -i postgres16 psql -U postgres -d sbavito < scripts/ochistka_drizzle.sql
--
-- ВАЖНО: скрипт трогает только БД sbavito. Соседние проекты общего контейнера
-- postgres16 живут в других БД (mydb, landing_bot) и не затрагиваются.

begin;

drop table if exists public.product_prices  cascade;
drop table if exists public.product_aliases cascade;
drop table if exists public.products        cascade;
drop table if exists public.categories      cascade;
drop table if exists public.faq             cascade;
drop table if exists public.knowledge_blocks cascade;
drop table if exists public.account_prompts cascade;
drop table if exists public.prompt_base     cascade;
drop table if exists public.accounts        cascade;

-- Enum'ы старой схемы (у новых те же имена, но они лежат в схеме sbavito).
drop type if exists public.account_kind;
drop type if exists public.availability;
drop type if exists public.price_unit;

-- Журнал миграций Drizzle.
drop schema if exists drizzle cascade;

commit;
