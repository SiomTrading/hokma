import { registerScreen } from "@/shell/registry";
import { ConnectionsScreen } from "./ConnectionsScreen";

// Not keepAlive: the card state is fetched when the screen opens and kept current by the live status messages.
registerScreen({ id: "connections", component: ConnectionsScreen });
