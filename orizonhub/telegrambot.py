#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import logging

from .utils import timestring_a, smartname, fwd_to_text
from .model import __version__, Protocol, Message, User, UserType, Response

import requests

logger = logging.getLogger('tgbot')

re_ircfmt = re.compile('([\x02\x1D\x1F\x16\x0F]|\x03(?:\d+(?:,\d+)?)?)')
re_mdescape = re.compile(r'([\[\*_])')
mdescape = lambda s: re_mdescape.sub(r'\\\1', s)

def ircfmt2tgmd(s):
    '''
    Convert IRC format code to Telegram Bot API style Markdown below:
        *bold text*
        _italic text_
        [text](URL)
        `inline fixed-width code`
        ```pre-formatted fixed-width code block```
    '''
    table = ('\x02\x16\x1D\x1F\x03', '**__*')
    # bold, reverse, italics, underline, color
    state = [False]*5
    code = ''
    ret = []
    for chunk in re_ircfmt.split(s):
        if not chunk:
            pass
        elif chunk[0] in table[0]:
            idx = table[0].index(chunk[0])
            if chunk[0] == '\x03':
                state[idx] = bool(chunk[1:])
            else:
                state[idx] = not state[idx]
            newcode = ''
            for k, v in enumerate(state):
                if v:
                    newcode = table[1][k]
                    break
            if code != newcode:
                ret.append(code + newcode)
                code = newcode
        elif chunk[0] == '\x0F':
            state = [False]*5
            ret.append(code)
            code = ''
        else:
            ret.append(mdescape(chunk))
    if code:
        ret.append(code)
    return ''.join(ret)

class BotAPIFailed(Exception):
    pass

class TelegramBotProtocol(Protocol):
    # media fields may change in the future, so use the inverse.
    STATIC_FIELDS = frozenset(('message_id', 'from', 'date', 'chat', 'forward_from',
                    'forward_date', 'reply_to_message', 'text', 'entities', 'caption'))
    METHODS = frozenset((
        'getMe', 'sendMessage', 'forwardMessage', 'sendPhoto', 'sendAudio',
        'sendDocument', 'sendSticker', 'sendVideo', 'sendVoice', 'sendLocation',
        'sendChatAction', 'getUserProfilePhotos', 'getUpdates', 'setWebhook',
        'getFile', 'answerInlineQuery'
    ))
    BotAPIFailed = BotAPIFailed

    def __init__(self, config, bus):
        self.config = config
        self.cfg = config.protocols.telegrambot
        self.bus = bus
        self.pastebin = bus.pastebin
        self.url = 'https://api.telegram.org/bot%s/' % self.cfg.token
        self.url_file = 'https://api.telegram.org/file/bot%s/' % self.cfg.token
        self.hsession = requests.Session()
        self.useragent = 'OrizonHub/%s %s' % (__version__, self.hsession.headers["User-Agent"])
        self.hsession.headers["User-Agent"] = self.useragent
        self.run = True
        # If you're sending bulk notifications to multiple users, the API will not
        # allow more than 30 messages per second or so. Consider spreading out
        # notifications over large intervals of 8—12 hours for best results.
        # Also note that your bot will not be able to send more than 20 messages
        # per minute to the same group.
        self.rate = 20/60 # 1/30
        self.attempts = 2
        self.last_sent = 0
        # updated later
        self.identity = User(None, 'telegram', UserType.user,
                             int(self.cfg.token.split(':')[0]), self.cfg.username,
                             config.bot_fullname, None, config.bot_nickname)
        # auto updated
        self.dest = User(None, 'telegram', UserType.group, self.cfg.groupid,
                         None, config.group_name, None, config.group_name)

    def start_polling(self):
        self.identity = self._make_user(self.bot_api('getMe'))
        self.cfg.username = self.identity.username
        self.bus.handler.usernames.add(self.identity.username)
        while self.run:
            logger.debug('tgapi.offset: %s', self.bus.state.get('tgapi.offset', 0))
            try:
                updates = self.bot_api('getUpdates', offset=self.bus.state.get('tgapi.offset', 0), timeout=10)
            except Exception:
                logging.exception('TelegramBot: Get updates failed.')
                continue
            if updates:
                logging.debug('TelegramBot: messages coming.')
                maxupd = 0
                for upd in updates:
                    maxupd = max(maxupd, upd['update_id'])
                    if 'message' in upd:
                        self.bus.post(self._make_message(upd['message']))
                self.bus.state['tgapi.offset'] = maxupd + 1
            time.sleep(.2)

    def send(self, response: Response, protocol: str, forwarded: Message) -> Message:
        rinfo = response.info or {}
        kwargs = rinfo.get('telegrambot', {})
        kwargs.update(rinfo.get('media', {}))
        text = response.text
        if response.reply.protocol.startswith('telegram'):
            kwargs['reply_to_message_id'] = response.reply.pid
            withreplysrc = False
            chat_id = response.reply.chat.pid
        elif forwarded:
            kwargs['reply_to_message_id'] = forwarded.pid
            withreplysrc = False
            chat_id = forwarded.chat.pid
        else:
            withreplysrc = True
            chat_id = self.dest.pid
        rtype = rinfo.get('type')
        if rtype == 'markdown':
            kwargs['parse_mode'] = 'Markdown'
        # we have handled this in commands
        #elif rtype == 'forward':
            #fmsgs = rinfo['messages']
            #if (len(fmsgs) == 1 and fmsgs[0].pid
                #and fmsgs[0].protocol.startswith('telegram')):
                #try:
                    #m = self.bot_api('forwardMessage',
                        #chat_id=response.reply.chat.pid,
                        #from_chat_id=fmsgs[0].chat.id, message_id=fmsgs[0].pid,
                        #disable_notification=kwargs.get('disable_notification'))
                    #return self._make_message(m)
                #except BotAPIFailed:
                    #pass
            #text = fwd_to_text(fmsgs, self.bus.timezone)
        elif rtype in ('photo', 'audio', 'document', 'sticker', 'video', 'voice'):
            fn = rinfo['media'].get('_file')
            if fn:
                input_file = {rtype: (os.path.basename(fn), open(fn, 'rb'))}
            else:
                # kwargs[rtype] must be filled
                input_file = None
            m = self.bot_api('send' + rtype.capitalize(),
                chat_id=response.reply.chat.pid, input_file=input_file, **kwargs)
            return self._make_message(m)
        elif rtype == 'location':
            m = self.bot_api('sendLocation',
                chat_id=response.reply.chat.pid, **kwargs)
            return self._make_message(m)
        if withreplysrc:
            text = '%s: %s' % (smartname(response.reply.src), text)
        m = self.sendmsg(text, chat_id, **kwargs)
        return self._make_message(m)

    def forward(self, msg: Message, protocol: str) -> Message:
        if protocol.startswith('telegram'):
            try:
                m = self.bot_api('forwardMessage', chat_id=self.dest.pid,
                    from_chat_id=msg.chat.id, message_id=msg.pid)
                return self._make_message(m)
            except BotAPIFailed:
                pass
        if msg.fwd_src:
            text = '[%s] Fwd %s: %s' % (smartname(msg.src), smartname(msg.fwd_src), msg.text)
        elif msg.reply:
            text = '[%s] %s: %s' % (smartname(msg.src), smartname(msg.reply.src), msg.text)
        else:
            text = '[%s] %s' % (smartname(msg.src), msg.alttext or msg.text)
        if re_ircfmt.search(text):
            text = ircfmt2tgmd(text)
            parse_mode = 'Markdown'
        else:
            parse_mode = None
        m = self.bot_api('sendMessage', chat_id=self.dest.pid, text=text,
            parse_mode=parse_mode)
        return self._make_message(m)

    def status(self, dest, action):
        return self.bot_api('sendChatAction', chat_id=dest.pid, action=action)

    def close(self):
        self.run = False

    def bot_api(self, method, input_file=None, **params):
        wait = self.rate - time.perf_counter() + self.last_sent
        if wait > 0:
            time.sleep(wait)
        att = 1
        ret = None
        while att <= self.attempts and self.run:
            try:
                req = self.hsession.post(self.url + method, data=params,
                                         files=input_file, timeout=45)
                retjson = req.content
                ret = json.loads(retjson.decode('utf-8'))
                break
            except Exception as ex:
                if att < self.attempts:
                    time.sleep((att+1) * 2)
                    self.change_session()
                else:
                    raise ex
            att += 1
        self.last_sent = time.perf_counter()
        if ret is None:
            raise BotAPIFailed('attempt = %s, self.run = %s', att, self.run)
        elif not ret['ok']:
            raise BotAPIFailed(str(ret))
        return ret['result']

    def sendmsg(self, text, chat_id, reply_to_message_id=None, **kwargs):
        text = text.strip()
        if not text:
            logging.warning('Empty message ignored: %s, %s' % (chat_id, reply_to_message_id))
            return
        logging.info('sendMessage(%s): %s' % (len(text), text[:20]))
        # 0-4096 characters.
        if len(text) > 2048:
            text = text[:2047] + '…'
        reply_id = reply_to_message_id or None
        return self.bot_api('sendMessage', chat_id=chat_id, text=text, reply_to_message_id=reply_id, **kwargs)

    def __getattr__(self, name):
        if name in self.METHODS:
            return lambda **kwargs: self.bot_api(name, **kwargs)
        else:
            raise AttributeError

    def _parse_media(self, media):
        mt = media.keys() & frozenset(('audio', 'document', 'sticker', 'video', 'voice'))
        file_ext = ''
        if mt:
            mt = mt.pop()
            file_id = media[mt]['file_id']
            file_size = media[mt].get('file_size')
            if mt == 'sticker':
                file_ext = '.webp'
        elif 'photo' in media:
            photo = max(media['photo'], key=lambda x: x['width'])
            file_id = photo['file_id']
            file_size = photo.get('file_size')
            file_ext = '.jpg'
        else:
            return None
        logging.debug('getFile: %r' % file_id)
        fp = self.bot_api('getFile', file_id=file_id)
        file_size = fp.get('file_size') or file_size or 0
        file_path = fp.get('file_path')
        if not file_path:
            raise BotAPIFailed("can't get file_path for " + file_id)
        file_ext = os.path.splitext(file_path)[1] or file_ext
        cachename = file_id + file_ext
        return (self.url_file + file_path, cachename, file_size)

    def servemedia(self, media):
        '''
        Reply type and link of media. This only generates links for photos.
        '''
        if not media:
            return ''
        ftype, fval = tuple(media.items())[0]
        ret = '<%s>' % ftype
        if 'new_chat_title' in media:
            ret += ' ' + media['new_chat_title']
        else:
            if ftype == 'document':
                ret += ' %s' % (fval.get('file_name', ''))
            elif ftype in ('video', 'voice'):
                ret += ' ' + timestring_a(fval.get('duration', 0))
            elif ftype == 'location':
                ret += ' https://www.openstreetmap.org/?mlat=%s&mlon=%s' % (
                        fval['latitude'], fval['longitude'])
            elif ftype == 'sticker' and fval.get('emoji'):
                ret = fval['emoji'] + ' ' + ret
            try:
                ret += ' ' + self.bus.pastebin.paste_url(*self._parse_media(media))
            except (TypeError, NotImplementedError):
                # _parse_media returned None
                pass
            except Exception:
                # ValueError, FileNotFoundError or network problems
                logging.exception("can't paste a file: %s", media)
        return ret

    def _make_message(self, obj):
        if obj is None:
            return None
        chat = self._make_user(obj['chat'])
        media = {k:obj[k] for k in frozenset(obj.keys()).difference(self.STATIC_FIELDS)}
        if chat.pid == self.dest.pid:
            self.dest = chat
            mtype = 'group'
        elif chat.type == UserType.user:
            mtype = 'private'
        else:
            # other group or channel
            mtype = 'othergroup'
        text = obj.get('text') or obj.get('caption', '')
        alttext = self.servemedia(media)
        if alttext and text:
            alttext = text + ' ' + alttext
        return Message(
            None, 'telegrambot', obj['message_id'],
            # from: Optional. Sender, can be empty for messages sent to channels
            self._make_user(obj.get('from') or obj['chat']),
            chat, text, media, obj['date'],
            self._make_user(obj.get('forward_from')), obj.get('forward_date'),
            self._make_message(obj.get('reply_to_message')), mtype, alttext or None
        )

    @staticmethod
    def _make_user(obj):
        if obj is None:
            return None
        if 'type' in obj:
            if obj['type'] == 'private':
                utype = UserType.user
            elif obj['type'] in ('group', 'supergroup'):
                utype = UserType.group
            else:
                utype = UserType.channel
        else:
            utype = UserType.user        
        return User(None, 'telegram', utype, obj['id'], obj.get('username'),
                    obj.get('first_name') or obj.get('title'),
                    obj.get('last_name'), None)

    def change_session(self):
        self.hsession.close()
        self.hsession = requests.Session()
        self.hsession.headers["User-Agent"] = self.useragent
        logging.warning('Session changed.')
