import {
  pgTable,
  pgEnum,
  serial,
  integer,
  varchar,
  text,
  boolean,
  numeric,
  timestamp,
  index,
  uniqueIndex,
  customType,
  type AnyPgColumn,
} from 'drizzle-orm/pg-core';
import { sql } from 'drizzle-orm';

/**
 * Схема БД SBAvito: прайс (per аккаунт) + промпты/база знаний.
 * Ключевые решения (см. 02_разбор_прайса.md, 06_ии_ядро.md):
 *  - одна строка = одна позиция, наличие — ЯВНЫЙ enum (пусто → unknown, не «нет»);
 *  - цена всегда с единицей; несколько цен на позицию → отдельная таблица product_prices;
 *  - синонимы для поиска → product_aliases; FTS + триграммы, чтобы бот находил, а не врал «нет в базе».
 */

// tsvector нет в базовых типах drizzle — объявляем кастомный.
const tsvector = customType<{ data: string }>({
  dataType() {
    return 'tsvector';
  },
});

// ── Enum ──────────────────────────────────────────────────────────────────
export const accountKind = pgEnum('account_kind', ['goods', 'services']);
export const availability = pgEnum('availability', [
  'in_stock',
  'out',
  'on_order',
  'unknown',
]);
export const priceUnit = pgEnum('price_unit', [
  'piece', // штука
  'm2', // квадратный метр
  'linear_m', // метр погонный
  'm3', // кубометр
  'set', // комплект
]);

// ── Аккаунты (3 авито-аккаунта) ─────────────────────────────────────────────
export const accounts = pgTable('accounts', {
  id: serial('id').primaryKey(),
  code: varchar('code', { length: 64 }).notNull().unique(),
  title: varchar('title', { length: 255 }).notNull(),
  kind: accountKind('kind').notNull(),
  isActive: boolean('is_active').notNull().default(true),
  createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp('updated_at', { withTimezone: true })
    .notNull()
    .defaultNow()
    .$onUpdate(() => new Date()),
});

// ── Категории (дерево, per аккаунт) ─────────────────────────────────────────
export const categories = pgTable(
  'categories',
  {
    id: serial('id').primaryKey(),
    accountId: integer('account_id')
      .notNull()
      .references(() => accounts.id, { onDelete: 'cascade' }),
    parentId: integer('parent_id').references((): AnyPgColumn => categories.id, {
      onDelete: 'set null',
    }),
    title: varchar('title', { length: 255 }).notNull(),
    sort: integer('sort').notNull().default(0),
    createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index('categories_account_idx').on(t.accountId)],
);

// ── Товары/позиции (плоский прайс) ──────────────────────────────────────────
export const products = pgTable(
  'products',
  {
    id: serial('id').primaryKey(),
    accountId: integer('account_id')
      .notNull()
      .references(() => accounts.id, { onDelete: 'cascade' }),
    categoryId: integer('category_id').references(() => categories.id, {
      onDelete: 'set null',
    }),
    article: varchar('article', { length: 128 }),
    name: text('name').notNull(),
    grade: varchar('grade', { length: 128 }), // сорт
    size: varchar('size', { length: 255 }),
    availability: availability('availability').notNull().default('unknown'),
    stockQty: numeric('stock_qty'),
    note: text('note'),
    isActive: boolean('is_active').notNull().default(true),
    // FTS-вектор (russian) по имени/сорту/размеру — генерируется Postgres.
    searchVector: tsvector('search_vector').generatedAlwaysAs(
      sql`to_tsvector('russian', coalesce(name, '') || ' ' || coalesce(grade, '') || ' ' || coalesce(size, ''))`,
    ),
    createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp('updated_at', { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
    updatedBy: varchar('updated_by', { length: 128 }),
  },
  (t) => [
    index('products_account_idx').on(t.accountId),
    index('products_search_idx').using('gin', t.searchVector),
    index('products_name_trgm_idx').using('gin', sql`${t.name} gin_trgm_ops`),
  ],
);

// ── Цены позиции (несколько единиц на одну позицию) ─────────────────────────
export const productPrices = pgTable(
  'product_prices',
  {
    id: serial('id').primaryKey(),
    productId: integer('product_id')
      .notNull()
      .references(() => products.id, { onDelete: 'cascade' }),
    unit: priceUnit('unit').notNull(),
    price: numeric('price', { precision: 12, scale: 2 }).notNull(),
    isDefault: boolean('is_default').notNull().default(false),
    createdAt: timestamp('created_at', { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index('product_prices_product_idx').on(t.productId)],
);

// ── Синонимы/народные названия для поиска ───────────────────────────────────
export const productAliases = pgTable(
  'product_aliases',
  {
    id: serial('id').primaryKey(),
    productId: integer('product_id')
      .notNull()
      .references(() => products.id, { onDelete: 'cascade' }),
    alias: varchar('alias', { length: 255 }).notNull(),
  },
  (t) => [
    uniqueIndex('product_aliases_uq').on(t.productId, t.alias),
    index('product_aliases_trgm_idx').using('gin', sql`${t.alias} gin_trgm_ops`),
  ],
);

// ── Базовый промпт (общие правила для всех аккаунтов) ────────────────────────
export const promptBase = pgTable('prompt_base', {
  id: serial('id').primaryKey(),
  version: integer('version').notNull().default(1),
  body: text('body').notNull(),
  isActive: boolean('is_active').notNull().default(true),
  updatedAt: timestamp('updated_at', { withTimezone: true })
    .notNull()
    .defaultNow()
    .$onUpdate(() => new Date()),
  updatedBy: varchar('updated_by', { length: 128 }),
});

// ── Промпт-надстройка на аккаунт (товары ≠ услуги ≠ бюджет) ──────────────────
export const accountPrompts = pgTable(
  'account_prompts',
  {
    id: serial('id').primaryKey(),
    accountId: integer('account_id')
      .notNull()
      .references(() => accounts.id, { onDelete: 'cascade' }),
    version: integer('version').notNull().default(1),
    persona: varchar('persona', { length: 128 }),
    body: text('body').notNull(),
    isActive: boolean('is_active').notNull().default(true),
    updatedAt: timestamp('updated_at', { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
    updatedBy: varchar('updated_by', { length: 128 }),
  },
  (t) => [index('account_prompts_account_idx').on(t.accountId)],
);

// ── База знаний (компания, доставка, гарантии, возражения) ───────────────────
export const knowledgeBlocks = pgTable(
  'knowledge_blocks',
  {
    id: serial('id').primaryKey(),
    accountId: integer('account_id')
      .notNull()
      .references(() => accounts.id, { onDelete: 'cascade' }),
    key: varchar('key', { length: 128 }).notNull(),
    title: varchar('title', { length: 255 }).notNull(),
    content: text('content').notNull(),
    sort: integer('sort').notNull().default(0),
    isActive: boolean('is_active').notNull().default(true),
    updatedAt: timestamp('updated_at', { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
  },
  (t) => [uniqueIndex('knowledge_blocks_uq').on(t.accountId, t.key)],
);

// ── FAQ (вопрос/ответ, per аккаунт) ─────────────────────────────────────────
export const faq = pgTable(
  'faq',
  {
    id: serial('id').primaryKey(),
    accountId: integer('account_id')
      .notNull()
      .references(() => accounts.id, { onDelete: 'cascade' }),
    question: text('question').notNull(),
    answer: text('answer').notNull(),
    sort: integer('sort').notNull().default(0),
    isActive: boolean('is_active').notNull().default(true),
    updatedAt: timestamp('updated_at', { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
  },
  (t) => [index('faq_account_idx').on(t.accountId)],
);
