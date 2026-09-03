import { signal } from "@preact/signals";

export interface ImportModalConfig {
  onJobImported?: (jobId: number) => void;
}

export type ModalType = "import_job" | null;

export const activeModal = signal<ModalType>(null);
export const importModalConfig = signal<ImportModalConfig>({});

export function openImportModal(config?: ImportModalConfig) {
  importModalConfig.value = config ?? {};
  activeModal.value = "import_job";
}

export function closeModal() {
  activeModal.value = null;
}
