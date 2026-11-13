import { defineMessages } from "@/i18n";

export default defineMessages({
  es: {
    "settings.telegram": "Telegram",
    "telegram.status.off": "Sin conectar",
    "telegram.status.waiting": "Esperando el código",
    "telegram.status.linked": "Vinculado",
    "telegram.linkedTo": "Chat de {name}",
    "telegram.token": "Token del bot",
    "telegram.token.hint": "Créalo con @BotFather en Telegram y pega aquí el token que te da.",
    "telegram.connect": "Conectar",
    "telegram.instruction": "Escríbele cualquier mensaje a tu bot en Telegram. El bot te responde con un código: escríbelo aquí.",
    "telegram.code": "Código",
    "telegram.approve": "Aprobar",
    "telegram.unlink": "Desvincular",
  },
  en: {
    "settings.telegram": "Telegram",
    "telegram.status.off": "Not connected",
    "telegram.status.waiting": "Waiting for the code",
    "telegram.status.linked": "Linked",
    "telegram.linkedTo": "{name}'s chat",
    "telegram.token": "Bot token",
    "telegram.token.hint": "Create it with @BotFather in Telegram and paste the token it gives you here.",
    "telegram.connect": "Connect",
    "telegram.instruction": "Send any message to your bot in Telegram. The bot replies with a code: type it here.",
    "telegram.code": "Code",
    "telegram.approve": "Approve",
    "telegram.unlink": "Unlink",
  },
});
