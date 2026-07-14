import { drizzle } from 'drizzle-orm/postgres-js';
import postgres from 'postgres';
import { env } from '../config/env.js';
import * as schema from './schema.js';

/**
 * Пул соединений с Postgres. Один на процесс.
 * Для миграций/сидов используется короткоживущее соединение (max: 1) — см. migrate.ts/seed.ts.
 */
export const sql = postgres(env.databaseUrl);

export const db = drizzle(sql, { schema });

export { schema };
