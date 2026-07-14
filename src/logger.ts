import pino from 'pino';
import { env } from './config/env.js';

/**
 * Единый pino-логгер проекта. Полный слой логов (см. 06_ии_ядро.md):
 * каждое действие пишется на русском, читаемо, с временем.
 */
export const logger = pino({
  level: env.logLevel,
  transport: {
    target: 'pino-pretty',
    options: {
      colorize: true,
      translateTime: 'dd.mm.yyyy HH:MM:ss',
      ignore: 'pid,hostname',
    },
  },
});
