import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
function execute(file, args, {input, ...options}) {
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, options, (error, stdout) => {
      if (error) reject(error);
      else resolve({stdout});
    });
    child.stdin.on('error', () => {}); // EPIPE is reported by execFile's callback.
    child.stdin.end(input);
  });
}
const number = /^[1-9][0-9]{0,29}$/;
const command = /^\/mail_correct(?:\s|$|@)/;

export function createHandler(options, config, run = execute) {
  if (options.enabled !== true) return undefined;
  if (![options.assistantRoot, options.assistantConfig, options.database, options.python ?? '/usr/bin/python3'].every(
    p => typeof p === 'string' && isAbsolute(p))) throw new Error('Absolute local bridge paths required');
  if (config.correction_bridge_enabled !== true ||
      typeof config.telegram_account_id !== 'string' ||
      !/^[A-Za-z0-9_-]{1,80}$/.test(config.telegram_account_id) ||
      /albert/i.test(config.telegram_account_id) ||
      ![config.telegram_chat_id, config.telegram_user_id].every(x => typeof x === 'string' && number.test(x)) ||
      config.telegram_chat_id !== config.telegram_user_id) throw new Error('Private non-Albert bridge config required');
  // Freeze only necessary routing fields, not the full config or secrets.
  const route = Object.freeze({account: config.telegram_account_id, chat: config.telegram_chat_id,
    user: config.telegram_user_id});
  const paths = Object.freeze({python: options.python ?? '/usr/bin/python3',
    config: options.assistantConfig, database: options.database, root: options.assistantRoot});
  return async (event, ctx) => {
    if (typeof event?.content !== 'string' || !command.test(event.content)) return undefined;
    // This plugin must not intercept Albert or another configured bot/channel.
    if (ctx?.accountId && ctx.accountId !== route.account) return undefined;
    if (ctx?.channelId && ctx.channelId !== 'telegram') return undefined;
    // Claim reserved commands silently on identity failure; never hand them to a model.
    if (event.channel !== 'telegram' || ctx?.channelId !== 'telegram' ||
        event.isGroup !== false || ctx.accountId !== route.account ||
        ctx.conversationId !== route.chat || ctx.senderId !== route.user || event.senderId !== route.user ||
        typeof ctx.messageId !== 'string' || !number.test(ctx.messageId) || event.messageId !== ctx.messageId) {
      return {handled: true};
    }
    if (event.content.length > 2048) return {handled: true, text: 'Corrección rechazada: comando demasiado largo.'};
    const envelope = {event: {channel: event.channel, isGroup: event.isGroup, senderId: event.senderId,
      messageId: event.messageId, content: event.content}, context: {channelId: ctx.channelId,
      accountId: ctx.accountId, conversationId: ctx.conversationId, senderId: ctx.senderId, messageId: ctx.messageId}};
    try {
      const {stdout} = await run(paths.python, [join(paths.root, 'bridge.py'), '--config', paths.config, '--db', paths.database], {
        input: JSON.stringify(envelope), timeout: 8000, maxBuffer: 4096, windowsHide: true,
        shell: false, cwd: paths.root,
      });
      const result = JSON.parse(stdout);
      if (!['recorded_locally', 'already_recorded'].includes(result.status) || result.external_writes !== 0) {
        throw new Error('Invalid local receipt');
      }
      return {handled: true, text: result.status === 'already_recorded' ?
        'Esta corrección ya estaba registrada; no se repitió.' :
        'Corrección registrada localmente. No se ejecutó ninguna acción externa.'};
    } catch {
      return {handled: true, text: 'No se confirmó la corrección. Revisá el estado local antes de reenviarla.'};
    }
  };
}

export default {
  id: 'bichi-mail-corrections',
  name: 'Bichi Mail Corrections',
  register(api) {
    const options = api.pluginConfig ?? {};
    if (options.enabled !== true) return;
    // No network, polling, service, file watcher or messages at registration/import.
    const config = JSON.parse(readFileSync(options.assistantConfig, 'utf8'));
    api.on('before_dispatch', createHandler(options, config));
  },
};
