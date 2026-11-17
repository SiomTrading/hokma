# fake_botapi

A local double of the Telegram Bot API (FastAPI). It answers the calls Hokma makes (`getMe`, `getUpdates`,
`sendMessage`, `createForumTopic`, `answerCallbackQuery`, `editMessageText`...) and lets a test drive it.

Never use a real bot token with it. The only tokens it accepts are the two fake ones in `VALID`.

## Start it

    python -m support.fake_botapi.botapi 47202        # from backend/tests; binds 127.0.0.1 only

## Point Hokma at it

Hokma talks to `https://api.telegram.org` unless `HOKMA_TELEGRAM_API_BASE` is set. The variable is for tests
only and accepts nothing but `http://127.0.0.1:<port>` or `http://localhost:<port>`:

    HOKMA_TELEGRAM_API_BASE=http://127.0.0.1:47202 hokma ...

## Control endpoints

    POST /_ctl/push     {...update}          queue an update for getUpdates
    POST /_ctl/revoke   {"token": "..."}     make a token answer 401
    POST /_ctl/net      {"down": true}       answer 502 to everything
    POST /_ctl/perm     {"ok": false}        refuse createForumTopic (no rights)
    POST /_ctl/reset                         forget what was sent
    GET  /_ctl/state                         sent, answers, edits, topics, calls, revoked
