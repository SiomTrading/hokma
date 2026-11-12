import { registerScreen } from "@/shell/registry";
import { LibraryScreen } from "./LibraryScreen";

// Not keepAlive: the list is read when the screen opens, and the search state is followed only while it is visible.
registerScreen({ id: "library", component: LibraryScreen });
