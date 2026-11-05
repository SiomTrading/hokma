import { registerScreen, registerSlot } from "@/shell/registry";
import { ChatScreen } from "./ChatScreen";
import { ConversationList } from "./ConversationList";

// keepAlive: a turn that is streaming keeps its state while another tab is in front.
registerScreen({ id: "chat", component: ChatScreen, keepAlive: true });

registerSlot("sidebar.conversations", ConversationList, { order: 0 });
