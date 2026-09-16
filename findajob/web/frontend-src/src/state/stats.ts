import { signal } from "@preact/signals";
import type { Stats } from "../api/types";

// Shared dashboard counters, updated in place after status changes.
export const stats = signal<Stats | null>(null);
