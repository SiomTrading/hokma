import type { ReactNode } from "react";
import { useId } from "react";
import { ApiError } from "@/api";
import type { QueryState } from "@/api";
import { ErrorState, Loading, Pane } from "@/design";
import { cx } from "@/design";

interface BlockProps {
  title: string;
  /** Short id used by the tests and by links inside the screen. */
  name: string;
  className?: string;
  actions?: ReactNode;
  children: ReactNode;
}

/** One block of the screen: a titled region. No animation: the screen is looked at many times a day. */
export function Block({ title, name, className, actions, children }: BlockProps) {
  const id = useId();
  return (
    <Pane as="section" className={cx("bt-block", className)} aria-labelledby={id} data-block={name}>
      <header className="bt-block-head">
        <h2 id={id} className="h2">
          {title}
        </h2>
        {actions}
      </header>
      <div className="bt-block-body">{children}</div>
    </Pane>
  );
}

export function isMissing(error: ApiError | undefined): boolean {
  return error?.status === 404;
}

interface QueryViewProps<T> {
  state: QueryState<T>;
  /** What to show when the server says there is nothing (404). */
  empty: ReactNode;
  children: (data: T) => ReactNode;
}

/** Loading, error, empty and data of one query, the same way in every block. */
export function QueryView<T>({ state, empty, children }: QueryViewProps<T>) {
  if (state.loading && state.data === undefined) return <Loading inline />;
  if (state.error) {
    if (isMissing(state.error)) return <>{empty}</>;
    return <ErrorState message={state.error.message} onRetry={state.reload} />;
  }
  if (state.data === undefined) return <>{empty}</>;
  return <>{children(state.data)}</>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="p bt-empty">{children}</p>;
}
