import { signal } from "@preact/signals";

export interface AppError {
  what: string;
  detail: string;
}

export const appErrors = signal<AppError[]>([]);

export function dismissAppErrors(): void {
  appErrors.value = [];
}
