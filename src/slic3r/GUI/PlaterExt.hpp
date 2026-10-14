#pragma once

#include <string>
#include <vector> // elegoo-mcp

namespace Slic3r {

class DynamicPrintConfig;
struct PrinterMmsGroup; // elegoo-mcp

namespace GUI::PlaterExt {

void sync_mms_filament();
int auto_load_missing_vendor_presets(DynamicPrintConfig& config, const std::string& filename);

// elegoo-mcp: the CANVAS sync of sync_mms_filament() without its dialogs, in two steps, so the
// Remote API (POST /printers/{id}/canvas/sync) runs the same code as the Sync button. The
// button shows its dialogs between the two steps, as it always did. GUI thread only.
struct MmsSyncUnknownTray
{
    std::string tray_name;     // "A1"
    std::string filament_type; // as the printer reported it
    std::string reason;        // PresetBundle::sync_ams_list's message, untranslated
};
struct MmsFilamentSync
{
    unsigned int                    count = 0; // filament slots after step 1; 0: nothing synced, project unchanged
    std::vector<MmsSyncUnknownTray> unknowns;  // trays synced with a fallback preset
    // The slots before step 1, for the flush volumes recalculated in step 2.
    std::vector<std::string>        color_before_sync;
    std::vector<bool>               is_support_before;
};
// Step 1: replaces the project's filament presets and colours with the CANVAS trays
// (color_only: only the colours), and remembers the trays' filament ids for the printer.
MmsFilamentSync start_mms_filament_sync(const PrinterMmsGroup& mms, bool color_only);
// Step 2 (only when step 1 synced something): updates the sidebar, selects the first filament
// preset in the Filament tab and recalculates flush volumes. Selecting the preset opens
// UnsavedChangesDialog when the Filament tab's preset has unsaved changes.
void finish_mms_filament_sync(const MmsFilamentSync& sync);

} // namespace GUI::PlaterExt
} // namespace Slic3r
