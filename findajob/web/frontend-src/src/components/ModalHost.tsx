import { activeModal, closeModal, importModalConfig } from "../state/modal";
import { ImportJobModal } from "./ImportJobModal";

export function ModalHost() {
  const currentModal = activeModal.value;

  if (!currentModal) return null;

  return (
    <>
      {currentModal === "import_job" && (
        <ImportJobModal
          isOpen={true}
          onClose={closeModal}
          onJobImported={(jobId) => {
            const callback = importModalConfig.value.onJobImported;
            closeModal();
            callback?.(jobId);
          }}
        />
      )}
    </>
  );
}
