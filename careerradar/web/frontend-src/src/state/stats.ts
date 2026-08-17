import { signal } from "@preact/signals";
import type { Stats } from "../api/types";

// Read by the 4 dashboard stat tiles, patched in place after a status change instead of
// refetched -- replaces the two `hx-swap-oob` counter patches the Jinja drawer used to send
// back on every status POST.
export const stats = signal<Stats | null>(null);
