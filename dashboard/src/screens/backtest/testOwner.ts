/** Test helper: reads the owner gate without rendering. */
import { renderHook } from "@testing-library/react";
import { useOwnerBlocked } from "./api";

export function useOwnerBlockedForTest(): boolean {
  return renderHook(() => useOwnerBlocked()).result.current;
}
