import { useToken } from "@/api";
import { NoToken, Shell } from "@/shell/Shell";
import { ShellProvider } from "@/shell/store";

export function App() {
  const token = useToken();
  if (!token) return <NoToken />;
  return (
    <ShellProvider>
      <Shell />
    </ShellProvider>
  );
}
