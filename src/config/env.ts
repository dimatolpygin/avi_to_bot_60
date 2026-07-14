import 'dotenv/config';

/** Обязательная переменная окружения — падаем сразу, если её нет. */
function required(name: string): string {
  const v = process.env[name];
  if (!v || v.trim() === '') {
    throw new Error(`Не задана переменная окружения ${name} (см. .env.example)`);
  }
  return v;
}

function optional(name: string, fallback: string): string {
  const v = process.env[name];
  return v && v.trim() !== '' ? v : fallback;
}

export const env = {
  databaseUrl: required('DATABASE_URL'),
  redisUrl: optional('REDIS_URL', 'redis://localhost:6379/0'),
  logLevel: optional('LOG_LEVEL', 'info'),
  openrouter: {
    apiKey: process.env.OPENROUTER_API_KEY ?? '',
    model: optional('OPENROUTER_MODEL', 'anthropic/claude-haiku-4.5'),
  },
  telegramBotToken: process.env.TELEGRAM_BOT_TOKEN ?? '',
  amo: {
    amojoId: process.env.AMOJO_ID ?? '',
    channelId: process.env.AMO_CHAT_CHANNEL_ID ?? '',
    channelSecret: process.env.AMO_CHAT_CHANNEL_SECRET ?? '',
  },
} as const;
