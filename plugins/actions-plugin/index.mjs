import {execFile} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {isAbsolute, join} from 'node:path';

function execute(file, args, input) {
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, {timeout: 8000, maxBuffer: 4096, shell: false}, (err, stdout) => {
      if (err) reject(err);
      else { try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); } }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(input));
  });
}

function nativeButtons(token, url, reviewed = false) {
  return [[{text: 'Abrir', url}, ...(reviewed ? [] : [
    {text: 'Revisado', callback_data: `bm2:done:${token}`},
    {text: 'Después', callback_data: `bm2:later:${token}`},
  ])]];
}

export function createHandler(options, run = execute) {
  if (![options.assistantRoot, options.assistantConfig, options.database].every(p => typeof p === 'string' && isAbsolute(p))) {
    throw new Error('Absolute mail action paths required');
  }
  return async ctx => {
    const config = JSON.parse(readFileSync(options.assistantConfig, 'utf8'));
    if (ctx.channel !== 'telegram' || ctx.isGroup !== false || ctx.auth?.isAuthorizedSender !== true ||
        ctx.accountId !== config.telegram_account_id || /albert/i.test(ctx.accountId) ||
        ctx.conversationId !== config.telegram_chat_id || ctx.callback?.chatId !== config.telegram_chat_id ||
        ctx.senderId !== config.telegram_user_id || config.telegram_chat_id !== config.telegram_user_id ||
        config.notification_actions_enabled !== true) return {handled: true};
    try {
      const result = await run('/usr/bin/python3', [join(options.assistantRoot, 'notification_actions.py'),
        '--config', options.assistantConfig, '--db', options.database], {
        channel: ctx.channel, account: ctx.accountId, chat: ctx.conversationId, user: ctx.senderId,
        authorized: ctx.auth.isAuthorizedSender, is_group: ctx.isGroup,
        callback_id: ctx.callbackId, message_id: String(ctx.callback.messageId), data: ctx.callback.data,
      });
      if (result.external_writes !== 0 || !/^[a-f0-9]{24}$/.test(result.token) ||
          !result.url?.startsWith('https://mail.google.com/mail/?authuser=')) throw new Error('Invalid receipt');
      if (result.status === 'later') {
        await ctx.respond.editButtons({buttons: [[
          {text: 'En 1 hora', callback_data: `bm2:1h:${result.token}`},
          {text: 'Mañana 08:30', callback_data: `bm2:tom:${result.token}`},
        ], [{text: 'Volver', callback_data: `bm2:cancel:${result.token}`}]]});
      } else {
        await ctx.respond.editButtons({buttons: nativeButtons(result.token, result.url, result.status === 'reviewed')});
        if (result.status === 'reviewed') await ctx.respond.reply({text: '✅ Marcado como revisado. Gmail no se modificó.'});
        else if (result.status === 'scheduled') await ctx.respond.reply({text: `⏰ Te lo recuerdo el ${result.due} (Salta).`});
      }
    } catch {
      await ctx.respond.reply({text: 'No pude confirmar la acción. Podés volver a intentarlo.'});
    }
    return {handled: true};
  };
}

export default {
  id: 'bichi-mail-actions', name: 'Bichi Mail Actions',
  register(api) {
    api.registerInteractiveHandler({channel: 'telegram', namespace: 'bm2', handler: createHandler(api.pluginConfig)});
  },
};
