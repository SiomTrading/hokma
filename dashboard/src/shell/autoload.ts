import { registerMessages } from "@/i18n";
import type { MessageModule } from "@/i18n";

/**
 * Loads what each delivery registers, without a shared file to edit:
 *  - texts:   src/i18n/messages/*.ts (default export)
 *  - screens: src/screens/<name>/register.ts(x) (side effects: registerScreen, registerSlot...)
 */
const messageModules = import.meta.glob<{ default: MessageModule }>(["../i18n/messages/*.ts"], { eager: true });

for (const mod of Object.values(messageModules)) registerMessages(mod.default);

import.meta.glob(["../screens/*/register.ts", "../screens/*/register.tsx"], { eager: true });
