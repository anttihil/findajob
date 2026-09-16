import { PipelineStatus } from "./PipelineStatus";
import { SyncTrigger } from "./SyncTrigger";
import { SearchTargetsWidget } from "../../components/SearchTargetsWidget";

export function SettingsPage() {
  return (
    <section class="tab-pane active">
      <div class="settings-container">
        <SyncTrigger />
        <PipelineStatus />

        <div class="mt-6">
          <SearchTargetsWidget initialExpanded={true} />
        </div>
      </div>
    </section>
  );
}
