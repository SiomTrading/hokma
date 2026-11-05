import { registerScreen } from "@/shell/registry";
import { HomeScreen } from "./HomeScreen";

registerScreen({ id: "panel", component: HomeScreen });
