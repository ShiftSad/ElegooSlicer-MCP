#include "PlaterExt.hpp"

#include "Plater.hpp"
#include "Elegoo/PrinterMmsSyncView.hpp"
#include "GUI.hpp"
#include "GUI_App.hpp"
#include "I18N.hpp"
#include "MainFrame.hpp"
#include "MsgDialog.hpp"
#include "PresetComboBoxes.hpp"
#include "Tab.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Utils.hpp"
#include "../Utils/PresetUpdater.hpp"

#include <boost/algorithm/string.hpp>
#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>

#include <map>
#include <memory>
#include <optional>
#include <vector>

namespace fs = boost::filesystem;

namespace Slic3r::GUI {

namespace {

std::string normalize_color(const std::string& color)
{
    std::string c = boost::algorithm::trim_copy(color);
    if (c.empty())
        return "#000000";
    if (c[0] != '#')
        c = "#" + c;
    if (c.size() > 7)
        c = c.substr(0, 7);
    boost::algorithm::to_lower(c);
    return c;
}

struct MmsFilamentSyncData
{
    std::map<int, DynamicPrintConfig> filament_list;
    std::string                       device_id;
};

// elegoo-mcp: the dialog part of the former load_mms_filament_sync_data; its conversion part
// is make_mms_filament_sync_data below, which the Remote API reaches without the dialog.
std::optional<PrinterMmsGroup> choose_mms_group_in_dialog()
{
    std::unique_ptr<PrinterMmsSyncView> view = std::make_unique<PrinterMmsSyncView>(wxGetApp().mainframe);
    if (view->ShowModal() != wxID_OK)
        return std::nullopt;

    return view->getSyncedMmsGroup();
}

// elegoo-mcp: the conversion part of the former load_mms_filament_sync_data, unchanged.
MmsFilamentSyncData make_mms_filament_sync_data(const PrinterMmsGroup& mms_info)
{
    MmsFilamentSyncData sync_data;
    int filament_index = 0;
    int mms_index      = 0;
    for (const PrinterMms& mms : mms_info.mmsList) {
        int slot_index = 0;
        for (const PrinterMmsTray& tray : mms.trayList) {
            const std::string color   = normalize_color(tray.filamentColor);
            const std::string mms_id  = !tray.mmsId.empty() ? tray.mmsId :
                                        (!mms.mmsId.empty() ? mms.mmsId : std::to_string(mms_index));
            const std::string slot_id = !tray.trayId.empty() ? tray.trayId : std::to_string(slot_index);

            DynamicPrintConfig cfg;
            cfg.set_key_value("filament_id",            new ConfigOptionStrings{tray.filamentId});
            cfg.set_key_value("filament_type",          new ConfigOptionStrings{tray.filamentType});
            cfg.set_key_value("filament_name",          new ConfigOptionStrings{tray.filamentName});
            cfg.set_key_value("filament_colour",        new ConfigOptionStrings{color});
            cfg.set_key_value("tray_name",              new ConfigOptionStrings{tray.trayName});
            cfg.set_key_value("filament_preset_name",   new ConfigOptionStrings{tray.filamentPresetName});
            cfg.set_key_value("filament_preset_alias",  new ConfigOptionStrings{tray.filamentPresetAlias});

            ConfigOptionStrings* multi_color = new ConfigOptionStrings{};
            multi_color->values.push_back(color);
            cfg.set_key_value("filament_multi_colour", multi_color);

            cfg.set_key_value("filament_colour_type",      new ConfigOptionStrings{"1"});
            cfg.set_key_value("ams_id",                    new ConfigOptionStrings{mms_id});
            cfg.set_key_value("slot_id",                   new ConfigOptionStrings{slot_id});
            cfg.set_key_value("filament_exist",            new ConfigOptionBools{true});
            cfg.set_key_value("filament_slot_placeholder", new ConfigOptionBools{false});

            sync_data.filament_list.emplace(filament_index, std::move(cfg));
            ++filament_index;
            ++slot_index;
        }
        ++mms_index;
    }

    sync_data.device_id = wxGetApp().preset_bundle->printers.get_edited_preset().base_id;
    return sync_data;
}

} // namespace

void PlaterExt::sync_mms_filament()
{
    wxBusyCursor cursor;

    Plater* plater = wxGetApp().plater();
    if (!plater)
        return;
    Sidebar& sidebar = plater->sidebar();

    std::optional<PrinterMmsGroup> mms = choose_mms_group_in_dialog(); // elegoo-mcp: see above
    if (!mms)
        return;

    // elegoo-mcp: the sync itself is start/finish_mms_filament_sync, shared with the Remote
    // API; the dialogs stay here, at the same points. The GUI always syncs presets and colours.
    MmsFilamentSync sync = start_mms_filament_sync(*mms, false);

    wxString detail;
    for (const MmsSyncUnknownTray& uk : sync.unknowns)
        detail += from_u8("\n- " + uk.tray_name + "(" + uk.filament_type + ") ") + _L(uk.reason);
    if (sync.count == 0) {
        MessageDialog dlg(&sidebar,
            _L("There are no compatible filaments, and sync is not performed.") + detail,
            _L("Sync filaments with MMS"), wxOK);
        dlg.ShowModal();
        return;
    }

    if (!sync.unknowns.empty()) {
        MessageDialog dlg(&sidebar,
            _L("There are some unknown filaments mapped to generic preset. Please update ElegooSlicer or restart ElegooSlicer to check if there is an update to system presets.") + detail,
            _L("Sync filaments with MMS"), wxOK);
        dlg.ShowModal();
    }

    finish_mms_filament_sync(sync);
}

// elegoo-mcp: the first half of the former sync_mms_filament body, unchanged except that the
// unknown trays are handed back instead of being shown here.
PlaterExt::MmsFilamentSync PlaterExt::start_mms_filament_sync(const PrinterMmsGroup& mms, bool color_only)
{
    MmsFilamentSync result;
    Sidebar& sidebar = wxGetApp().plater()->sidebar();
    MmsFilamentSyncData sync_data = make_mms_filament_sync_data(mms);

    if (wxGetApp().preset_bundle->filament_ams_list != sync_data.filament_list) {
        wxGetApp().preset_bundle->filament_ams_list = sync_data.filament_list;
        for (PlaterPresetComboBox* combo : sidebar.combos_filament())
            combo->update();
    }

    std::map<int, DynamicPrintConfig>& mms_filament_list = wxGetApp().preset_bundle->filament_ams_list;
    std::string mms_filament_ids = wxGetApp().app_config->get("ams_filament_ids", sync_data.device_id);
    std::vector<std::string> synced_filament_ids;
    if (!mms_filament_ids.empty())
        boost::algorithm::split(synced_filament_ids, mms_filament_ids, boost::algorithm::is_any_of(","));

    synced_filament_ids.resize(mms_filament_list.size());
    std::map<int, DynamicPrintConfig>::iterator iter = mms_filament_list.begin();
    for (size_t i = 0; i < mms_filament_list.size(); ++i, ++iter) {
        DynamicPrintConfig& mms_filament_config = iter->second;
        std::string         filament_id         = mms_filament_config.opt_string("filament_id", 0u);
        mms_filament_config.set_key_value("filament_changed", new ConfigOptionBool{true});
        synced_filament_ids[i] = filament_id;
    }

    std::vector<std::string>& color_before_sync = result.color_before_sync; // elegoo-mcp: kept for step 2
    std::vector<bool>&        is_support_before = result.is_support_before; // elegoo-mcp: kept for step 2
    DynamicPrintConfig&      project_config = wxGetApp().preset_bundle->project_config;
    ConfigOptionStrings*     color_opt      = project_config.option<ConfigOptionStrings>("filament_colour");
    std::vector<PlaterPresetComboBox*>& combos = sidebar.combos_filament();
    for (size_t i = 0; i < combos.size(); ++i) {
        is_support_before.push_back(is_support_filament(static_cast<int>(i)));
        color_before_sync.push_back(i < color_opt->values.size() ? color_opt->values[i] : std::string());
    }

    std::vector<std::pair<DynamicPrintConfig*, std::string>> unknowns;
    std::map<int, AMSMapInfo>                                maps;
    MergeFilamentInfo                                        merge_info;
    // elegoo-mcp: color_only comes from the caller (the GUI passes false, as it always did)
    unsigned int n = wxGetApp().preset_bundle->sync_ams_list(unknowns, false, maps, false, merge_info, color_only);
    result.count   = n;

    // elegoo-mcp: the caller lists these (the GUI in its dialogs)
    for (std::pair<DynamicPrintConfig*, std::string>& uk : unknowns)
        result.unknowns.push_back({uk.first->opt_string("tray_name", 0u), uk.first->opt_string("filament_type", 0u), uk.second});
    if (n == 0)
        return result;

    PresetCollection&         filaments        = wxGetApp().preset_bundle->filaments;
    std::vector<std::string>& filament_presets = wxGetApp().preset_bundle->filament_presets;
    std::map<int, DynamicPrintConfig>::iterator synced_iter = mms_filament_list.begin();
    for (size_t i = 0; i < synced_filament_ids.size() && synced_iter != mms_filament_list.end(); ++i, ++synced_iter) {
        if (synced_filament_ids[i].empty())
            synced_filament_ids[i] = synced_iter->second.opt_string("filament_id", 0u);
        if ((synced_filament_ids[i].empty() || synced_filament_ids[i] == UNKNOWN_FILAMENT_ID) && i < filament_presets.size()) {
            const Preset* resolved = filaments.find_preset(filament_presets[i]);
            if (resolved)
                synced_filament_ids[i] = resolved->filament_id;
        }
    }
    mms_filament_ids = boost::algorithm::join(synced_filament_ids, ",");
    wxGetApp().app_config->set("ams_filament_ids", sync_data.device_id, mms_filament_ids);
    return result;
}

// elegoo-mcp: the second half of the former sync_mms_filament body, unchanged.
void PlaterExt::finish_mms_filament_sync(const MmsFilamentSync& sync)
{
    Sidebar&                            sidebar           = wxGetApp().plater()->sidebar();
    const unsigned int                  n                 = sync.count;
    const std::vector<std::string>&     color_before_sync = sync.color_before_sync;
    const std::vector<bool>&            is_support_before = sync.is_support_before;
    ConfigOptionStrings*                color_opt         = wxGetApp().preset_bundle->project_config.option<ConfigOptionStrings>("filament_colour");
    std::vector<PlaterPresetComboBox*>& combos            = sidebar.combos_filament();

    wxGetApp().plater()->on_filament_count_change(n);
    for (PlaterPresetComboBox* combo : combos)
        combo->update();
    sidebar.update_filaments_area_height();

    wxGetApp().get_tab(Preset::TYPE_FILAMENT)->select_preset(wxGetApp().preset_bundle->filament_presets[0]);
    wxGetApp().preset_bundle->export_selections(*wxGetApp().app_config);
    sidebar.update_dynamic_filament_list();

    for (size_t i = 0; i < combos.size(); ++i) {
        if (i >= color_before_sync.size()) {
            sidebar.auto_calc_flushing_volumes(static_cast<int>(i));
        } else if (i < color_opt->values.size() &&
                   color_before_sync[i] != color_opt->values[i] &&
                   wxGetApp().app_config->get("auto_calculate_flush") != "disabled") {
            sidebar.auto_calc_flushing_volumes(static_cast<int>(i));
        } else if (is_support_filament(static_cast<int>(i)) != is_support_before[i] &&
                   wxGetApp().app_config->get("auto_calculate_flush") == "all") {
            sidebar.auto_calc_flushing_volumes(static_cast<int>(i));
        }
    }
    sidebar.Layout();
}

int PlaterExt::auto_load_missing_vendor_presets(DynamicPrintConfig& config, const std::string& filename)
{
    PresetBundle* preset_bundle = wxGetApp().preset_bundle;
    if (!preset_bundle) {
        BOOST_LOG_TRIVIAL(error) << "Auto-load missing vendor presets: preset_bundle is null for " << filename;
        return VALIDATE_PRESETS_PRINTER_NOT_FOUND;
    }

    std::string printer_preset = config.option<ConfigOptionString>("printer_settings_id", true)->value;
    BOOST_LOG_TRIVIAL(info) << "Auto-load missing vendor presets: " << filename << " " << printer_preset;
    if (printer_preset.empty()) {
        return VALIDATE_PRESETS_PRINTER_NOT_FOUND;
    }
    std::string printer_model   = config.option<ConfigOptionString>("printer_model", true)->value;
    std::string printer_variant = config.option<ConfigOptionString>("printer_variant", true)->value;

    if (printer_model.empty() || printer_variant.empty()) {
        BOOST_LOG_TRIVIAL(info) << "Auto-load missing vendor presets: " << filename << " " << printer_preset << " printer_model or printer_variant is empty";
        return VALIDATE_PRESETS_PRINTER_NOT_FOUND;
    }

    std::string vendor_name;

    PresetBundle temp_bundle;
    temp_bundle.load_system_models_from_json(ForwardCompatibilitySubstitutionRule::EnableSilent);
    for (const auto& vendor_pair : temp_bundle.vendors) {
        const auto& vendor_profile = vendor_pair.second;
        for (const auto& model : vendor_profile.models) {
            if (!printer_model.empty() && model.id == printer_model) {
                vendor_name = vendor_pair.first;
                break;
            }
        }
        if (!vendor_name.empty())
            break;
    }

    if (vendor_name.empty()) {
        BOOST_LOG_TRIVIAL(info) << "Auto-load missing vendor presets: " << filename << " " << printer_preset << " vendor_name is empty";
        return VALIDATE_PRESETS_PRINTER_NOT_FOUND;
    }

    const fs::path vendor_dir = (fs::path(Slic3r::data_dir()) / PRESET_SYSTEM_DIR).make_preferred();
    const fs::path vendor_file = vendor_dir / (vendor_name + ".json");

    if (!fs::exists(vendor_file)) {
        std::unique_ptr<PresetUpdater> updater = std::make_unique<PresetUpdater>();
        std::vector<std::string>       install_bundles;
        install_bundles.emplace_back(vendor_name);
        if (!updater->install_bundles_rsrc(std::move(install_bundles), false)) {
            BOOST_LOG_TRIVIAL(error) << "Failed to auto-install vendor bundle: " << vendor_name << " for " << filename << " " << printer_preset;
            return VALIDATE_PRESETS_PRINTER_NOT_FOUND;
        }
        BOOST_LOG_TRIVIAL(info) << "Auto-installing vendor bundle: " << vendor_name << " for " << filename << " " << printer_preset;
        AppConfig* app_config = wxGetApp().app_config;
        app_config->set_variant(vendor_name, printer_model, printer_variant, "true");
    }

    preset_bundle->load_presets(*wxGetApp().app_config, ForwardCompatibilitySubstitutionRule::Enable);
    return VALIDATE_PRESETS_SUCCESS;
}
} // namespace Slic3r::GUI
