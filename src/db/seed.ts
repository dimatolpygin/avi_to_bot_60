import { sql as dsql } from 'drizzle-orm';
import { db, sql } from './index.js';
import { accounts, promptBase, accountPrompts } from './schema.js';
import { logger } from '../logger.js';

/**
 * Идемпотентный seed: 3 авито-аккаунта + заглушка базового промпта + промпт-надстройки.
 * Повторный запуск не плодит дубли (аккаунты — по уникальному code, промпты — по наличию).
 */

const ACCOUNTS = [
  { code: 'saunamart', title: 'Saunamart', kind: 'goods' as const },
  { code: 'sbsauna', title: 'SB SAUNA', kind: 'services' as const },
  { code: 'otdelka3', title: 'Отделка (бюджетный акцент)', kind: 'services' as const },
];

const BASE_PROMPT = `Ты — живой продавец в переписке на Авито, не бот и не ассистент.
Пиши коротко, 1–2 предложения, как в мессенджере. Приветствие один раз.
Цены и наличие бери ТОЛЬКО из инструментов, никогда из памяти. Нет позиции — честно «уточню», не выдумывай.
Пустой остаток ≠ «нет в наличии». Всегда называй единицу измерения.
Запрещено: тире «—», «передам менеджеру», выпрашивание телефона, вываливание всего прайса, эмодзи, markdown, канцелярит.
Телефон не проси — бери контакт только когда клиент сам его дал или просит перезвонить.`;

async function seedAccounts() {
  for (const a of ACCOUNTS) {
    await db.insert(accounts).values(a).onConflictDoNothing({ target: accounts.code });
  }
  const rows = await db.select({ id: accounts.id, code: accounts.code }).from(accounts);
  logger.info(`👥 Аккаунтов в базе: ${rows.length} (${rows.map((r) => r.code).join(', ')})`);
  return rows;
}

async function seedBasePrompt() {
  const existing = await db.select({ id: promptBase.id }).from(promptBase);
  if (existing.length === 0) {
    await db.insert(promptBase).values({ body: BASE_PROMPT, version: 1, isActive: true });
    logger.info('📝 Базовый промпт создан (заглушка).');
  } else {
    logger.info('📝 Базовый промпт уже есть — пропускаю.');
  }
}

async function seedAccountPrompts(accountRows: { id: number; code: string }[]) {
  for (const acc of accountRows) {
    const has = await db
      .select({ id: accountPrompts.id })
      .from(accountPrompts)
      .where(dsql`${accountPrompts.accountId} = ${acc.id}`);
    if (has.length > 0) continue;
    await db.insert(accountPrompts).values({
      accountId: acc.id,
      persona: acc.code === 'saunamart' ? 'продавец товаров' : 'консультант по услугам',
      body:
        acc.code === 'saunamart'
          ? 'Аккаунт товаров (вагонка, печи, двери, камень). Помогаешь подобрать материал, называешь цену за единицу.'
          : acc.code === 'otdelka3'
            ? 'Аккаунт отделки с бюджетным акцентом. Мягко ведёшь к замеру/расчёту, подчёркиваешь доступную цену.'
            : 'Аккаунт услуг (отделка парных под ключ, дизайн-проект, монтаж). Ведёшь к замеру/расчёту.',
      version: 1,
      isActive: true,
    });
    logger.info(`📝 Промпт-надстройка создана для аккаунта ${acc.code}.`);
  }
}

async function main() {
  logger.info('🌱 Запускаю seed…');
  const accountRows = await seedAccounts();
  await seedBasePrompt();
  await seedAccountPrompts(accountRows);
  logger.info('✅ Seed завершён.');
  await sql.end();
}

main().catch(async (err) => {
  logger.error({ err }, '❌ Ошибка seed');
  await sql.end();
  process.exit(1);
});
