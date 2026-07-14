import { drizzle } from 'drizzle-orm/postgres-js';
import { migrate } from 'drizzle-orm/postgres-js/migrator';
import postgres from 'postgres';
import { env } from '../config/env.js';
import { logger } from '../logger.js';

/**
 * Применение миграций Drizzle. Отдельное короткоживущее соединение (max: 1).
 * Перед миграциями включаем расширения Postgres, которые нужны схеме
 * (pg_trgm — триграммный поиск по названиям товаров на этапе 4).
 */
async function main() {
  const migrationClient = postgres(env.databaseUrl, { max: 1 });
  try {
    logger.info('🧩 Включаю расширения Postgres (pg_trgm)…');
    await migrationClient`CREATE EXTENSION IF NOT EXISTS pg_trgm`;

    logger.info('⛓️  Применяю миграции Drizzle…');
    const db = drizzle(migrationClient);
    await migrate(db, { migrationsFolder: './drizzle' });

    logger.info('✅ Миграции применены (или их нет — это тоже ок).');
  } finally {
    await migrationClient.end();
  }
}

main().catch((err) => {
  logger.error({ err }, '❌ Ошибка применения миграций');
  process.exit(1);
});
