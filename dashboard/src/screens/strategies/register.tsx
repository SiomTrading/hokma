import { registerScreen } from "@/shell/registry";
import { StrategiesScreen } from "./StrategiesScreen";

// Title and icon are built in: see BUILTIN_SCREENS.
registerScreen({ id: "strategies", component: StrategiesScreen });
