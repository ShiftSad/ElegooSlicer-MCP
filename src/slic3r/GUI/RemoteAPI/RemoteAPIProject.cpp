// src/slic3r/GUI/RemoteAPI/RemoteAPIProject.cpp
//
// elegoo-mcp: project routes ElegooSlicer MCP adds to the Remote API: filament slots,
// split, undo/redo, layer changes and the purge part of the slice breakdown. Like
// RemoteAPIModelOps.cpp, every handler runs its GUI work inside run_on_ui and repeats
// the steps of the matching GUI action, leaving out that action's dialogs: an API call
// must never open a modal window, because nobody may be at the machine to close it.
#include "RemoteAPIController.hpp"

#include "libslic3r/AppConfig.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/libslic3r.h"          // MAXIMUM_EXTRUDER_NUMBER
#include "libslic3r/TriangleMesh.hpp"
#include "slic3r/GUI/GLCanvas3D.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/GUI_ObjectList.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/ObjectDataViewModel.hpp"
#include "slic3r/GUI/PartPlate.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/PresetComboBoxes.hpp"
#include "slic3r/GUI/Selection.hpp"
#include "slic3r/GUI/Tab.hpp"
#include "slic3r/Utils/UndoRedo.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// The window greys out project edits while a G-code file or an exported .gcode.3mf is
// open, and the filament +/- buttons would ask to close that file first
// (Sidebar::is_new_project_in_gcode3mf). An API call cannot answer, so these routes
// refuse instead, as POST /view and POST /project/save do. GUI thread only.
static bool preview_only()
{
    Plater *plater = wxGetApp().plater();
    return plater->only_gcode_mode() || plater->using_exported_file();
}

// True while a tool of the 3D view that keeps its own copy of the model data is open
// (paint, cut, text, measure...): it would write that copy back over an API edit or
// lose track of the model. Move, rotate and scale only act on the selection.
// GUI thread only.
static bool editing_gizmo_open()
{
    const GLGizmosManager      &gizmos = wxGetApp().plater()->get_view3D_canvas3D()->get_gizmos_manager();
    const GLGizmosManager::EType type  = gizmos.get_current_type();
    return gizmos.is_running() && type != GLGizmosManager::Move && type != GLGizmosManager::Rotate &&
           type != GLGizmosManager::Scale;
}

// Parses an index from a path segment ("3"); -1 when it is not a number >= 0.
static int parse_index(const std::string &s)
{
    try {
        size_t used = 0;
        const int value = std::stoi(s, &used);
        return used == s.size() ? value : -1;
    } catch (...) {
        return -1;
    }
}

std::optional<Response> Controller::route_project(const Request &req)
{
    const std::string path = req.target.substr(0, req.target.find('?'));
    static const std::string filaments = "/api/v1/filaments";
    try {
        if (path == filaments) {
            if (req.method == "GET") return handle_get_filaments();
            if (req.method == "PUT") return handle_put_filaments(req.body);
            return Response{ 404, {{"error", "not_found"}} };
        }
        if (path.rfind(filaments + "/", 0) == 0) {
            const int slot = parse_index(path.substr(filaments.size() + 1));
            if (slot < 0) return Response{ 400, {{"error", "bad_slot_index"}} };
            if (req.method == "PUT") return handle_put_filament_slot(slot, req.body);
            return Response{ 404, {{"error", "not_found"}} };
        }
        // /api/v1/objects/<id>/split; the other object routes stay in dispatch.
        static const std::string objects = "/api/v1/objects/";
        static const std::string split   = "/split";
        if (path.size() > objects.size() + split.size() && path.rfind(objects, 0) == 0 &&
            path.compare(path.size() - split.size(), split.size(), split) == 0 &&
            path.find('/', objects.size()) == path.size() - split.size()) {
            const std::string id_str = path.substr(objects.size(), path.size() - objects.size() - split.size());
            uint64_t id = 0;
            try {
                size_t used = 0;
                id = std::stoull(id_str, &used);
                if (used != id_str.size()) throw std::invalid_argument("trailing characters");
            } catch (const std::exception &) {
                return Response{ 400, {{"error", "bad_object_id"}} };
            }
            if (req.method == "POST") return handle_split_object(id, req.body);
            return Response{ 404, {{"error", "not_found"}} };
        }
        if (path == "/api/v1/undo" && req.method == "POST") return handle_undo_redo(true);
        if (path == "/api/v1/redo" && req.method == "POST") return handle_undo_redo(false);
        if (path == "/api/v1/calibration" && req.method == "POST") return handle_calibration(req.body);
        // /api/v1/plates/<i>/layer_changes; the other plate routes stay in dispatch.
        static const std::string plates        = "/api/v1/plates/";
        static const std::string layer_changes = "/layer_changes";
        if (path.size() > plates.size() + layer_changes.size() && path.rfind(plates, 0) == 0 &&
            path.compare(path.size() - layer_changes.size(), layer_changes.size(), layer_changes) == 0 &&
            path.find('/', plates.size()) == path.size() - layer_changes.size()) {
            const int plate = parse_index(path.substr(plates.size(), path.size() - plates.size() - layer_changes.size()));
            if (plate < 0) return Response{ 400, {{"error", "bad_plate_index"}} };
            if (req.method == "GET")    return handle_get_layer_changes(plate);
            if (req.method == "POST")   return handle_add_layer_change(plate, req.body);
            if (req.method == "DELETE") return handle_delete_layer_change(plate, req.target);
            return Response{ 404, {{"error", "not_found"}} };
        }
    } catch (const nlohmann::json::parse_error &) {
        return Response{ 400, {{"error", "invalid_json"}} };
    }
    return std::nullopt; // not one of these routes
}

// --- filament slots: /filaments -------------------------------------------------
// The sidebar's filament list: one slot per combo box, numbered from 1 like the object
// list's filament column and the "extruder" setting. A slot's preset is
// PresetBundle::filament_presets[slot - 1]; its colour is the project's
// filament_colour[slot - 1].

static std::string to_lower(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return (char) std::tolower(c); });
    return s;
}

static std::string slot_colour(size_t idx)
{
    const auto *colours = wxGetApp().preset_bundle->project_config.option<ConfigOptionStrings>("filament_colour");
    return colours != nullptr && idx < colours->values.size() ? to_lower(colours->values[idx]) : std::string();
}

// One slot as JSON. plate_slots: the 1-based slots the current plate prints with.
static nlohmann::json slot_json(size_t idx, const std::vector<int> &plate_slots)
{
    PresetBundle      *bundle = wxGetApp().preset_bundle;
    const std::string &name   = bundle->filament_presets[idx];
    std::string        type;
    if (const Preset *preset = bundle->filaments.find_preset(name, false); preset != nullptr) {
        const auto *types = preset->config.option<ConfigOptionStrings>("filament_type");
        if (types != nullptr && !types->values.empty())
            type = types->values.front();
    }
    const int slot = int(idx) + 1;
    return {
        {"slot", slot},
        {"preset", name},
        {"type", type},
        {"color", slot_colour(idx)},
        {"used_on_current_plate", std::find(plate_slots.begin(), plate_slots.end(), slot) != plate_slots.end()},
    };
}

static nlohmann::json filaments_json()
{
    // The plate's own list of slots: object, part, height-range and paint filaments,
    // and with true also the plate's layer tool changes.
    const std::vector<int> plate_slots = wxGetApp().plater()->get_partplate_list().get_curr_plate()->get_extruders(true);
    const size_t   count = wxGetApp().preset_bundle->filament_presets.size();
    nlohmann::json slots = nlohmann::json::array();
    for (size_t i = 0; i < count; ++i)
        slots.push_back(slot_json(i, plate_slots));
    return {{"count", count}, {"max", (int) MAXIMUM_EXTRUDER_NUMBER}, {"slots", slots}};
}

// The filament settings an object, a part or a height range can override; 0 means
// "the default", any other value is a 1-based slot.
static const char *const k_slot_keys[] = {
    "extruder", "support_filament", "support_interface_filament",
    "outer_wall_filament_id", "inner_wall_filament_id", "sparse_infill_filament_id",
    "internal_solid_filament_id", "top_surface_filament_id", "bottom_surface_filament_id",
};

// The first of k_slot_keys that cfg sets to a slot in [first, last] (slot_out gets
// it), or "" when none does.
static std::string key_using_slots(const DynamicPrintConfig &cfg, int first, int last, int &slot_out)
{
    for (const char *key : k_slot_keys) {
        const ConfigOption *opt = cfg.option(key);
        if (opt == nullptr) continue;
        const int slot = opt->getInt();
        if (slot >= first && slot <= last) {
            slot_out = slot;
            return key;
        }
    }
    return {};
}

// Everything in the model that prints with a slot in [first, last]: object, part and
// height-range filament settings, and colour paint. Removing such a slot moves those
// users to another filament (the GUI's delete does that), so PUT /filaments reports them
// first. Tool changes in the plates' layer changes are not listed: the GUI's delete
// simply drops the ones going to a removed slot.
static nlohmann::json slot_users(const Model &model, int first, int last)
{
    nlohmann::json users = nlohmann::json::array();
    for (const ModelObject *mo : model.objects) {
        const uint64_t oid  = mo->id().id;
        int            slot = 0;
        std::string    key  = key_using_slots(mo->config.get(), first, last, slot);
        if (!key.empty())
            users.push_back({{"object_id", oid}, {"name", mo->name}, {"slot", slot}, {"setting", key}});
        for (const auto &range : mo->layer_config_ranges) {
            key = key_using_slots(range.second.get(), first, last, slot);
            if (!key.empty())
                users.push_back({{"object_id", oid}, {"name", mo->name}, {"slot", slot}, {"setting", key},
                                 {"height_range", {range.first.first, range.first.second}}});
        }
        for (const ModelVolume *mv : mo->volumes) {
            key = key_using_slots(mv->config.get(), first, last, slot);
            if (!key.empty())
                users.push_back({{"object_id", oid}, {"name", mo->name}, {"volume_id", (uint64_t) mv->id().id},
                                 {"slot", slot}, {"setting", key}});
            // Colour paint stores 0-based filament indices here.
            for (size_t painted : mv->get_extruders_from_multi_material_painting()) {
                const int painted_slot = int(painted) + 1;
                if (painted_slot >= first && painted_slot <= last) {
                    users.push_back({{"object_id", oid}, {"name", mo->name}, {"volume_id", (uint64_t) mv->id().id},
                                     {"slot", painted_slot}, {"setting", "color_paint"}});
                    break; // one entry per part is enough to name it
                }
            }
        }
    }
    return users;
}

// After slots were removed from the end of the list: the GUI's delete moves the
// "extruder" and support filament settings off the removed slots, but leaves the
// per-feature ones (outer_wall_filament_id...) pointing at a slot that no longer
// exists. Put every setting still above the new count back to 0 ("default").
static void clear_removed_slot_settings(Model &model, int count)
{
    auto clear = [count](ModelConfig &cfg) {
        for (const char *key : k_slot_keys) {
            const ConfigOption *opt = cfg.get().option(key);
            if (opt != nullptr && opt->getInt() > count)
                cfg.erase(key);
        }
    };
    for (ModelObject *mo : model.objects) {
        clear(mo->config);
        for (auto &range : mo->layer_config_ranges)
            clear(range.second);
        for (ModelVolume *mv : mo->volumes)
            clear(mv->config);
    }
}

Response Controller::handle_get_filaments()
{
    nlohmann::json r = run_on_ui([]() -> nlohmann::json { return filaments_json(); });
    return { 200, r };
}

// PUT /filaments {"count": n, "force"?: bool}
// Adds or removes slots at the end of the list with the sidebar's own + and - buttons
// (Sidebar::add_filament / delete_filament, each followed by update_filaments_counter
// as the buttons do), one slot at a time.
Response Controller::handle_put_filaments(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_project
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (!in.contains("count") || !in["count"].is_number_integer())
        return { 422, {{"error", "bad_count"}, {"detail", "count: the number of filament slots, an integer"}} };
    if (in.contains("force") && !in["force"].is_boolean())
        return { 400, {{"error", "force_must_be_boolean"}} };
    const int  count = in["count"].get<int>();
    const bool force = in.value("force", false);
    if (count < 1 || count > (int) MAXIMUM_EXTRUDER_NUMBER)
        return { 422, {{"error", "bad_count"}, {"detail", "1 <= count <= " + std::to_string(MAXIMUM_EXTRUDER_NUMBER)}} };

    // Each slot rebuilds sidebar widgets and the flush matrix: allow 30 s, not 10.
    nlohmann::json r = run_on_ui([count, force]() -> nlohmann::json {
        if (preview_only()) return {{"error", "preview_only"}};
        Plater       *plater  = wxGetApp().plater();
        PresetBundle *bundle  = wxGetApp().preset_bundle;
        Sidebar      &sidebar = wxGetApp().sidebar();
        const int     before  = (int) bundle->filament_presets.size();
        if (count == before)
            return filaments_json();
        // The sidebar shows the +/- buttons only for single-extruder multi-material
        // printers (Sidebar::show_SEMM_buttons); other printers have one slot per
        // extruder, fixed by the printer preset.
        if (!Sidebar::should_show_SEMM_buttons())
            return {{"error", "bad_count"},
                    {"detail", "this printer preset has a fixed filament count (single_extruder_multi_material is off)"}};

        if (count < before) {
            const nlohmann::json users = slot_users(plater->model(), count + 1, before);
            if (!users.empty() && !force)
                return {{"error", "slot_in_use"}, {"used_by", users},
                        {"hint", "pass \"force\": true to remove the slots anyway; their users move to filament 1"}};
            // Removing the only slot that uses the preset open in the Filament tab makes
            // the GUI switch that tab to slot 1's preset (Sidebar::delete_filament), and
            // with unsaved edits in the tab that switch asks what to do with them
            // (UnsavedChangesDialog). Refuse instead of opening that question.
            if (bundle->filaments.current_is_dirty()) {
                const std::string edited = bundle->filaments.get_edited_preset().name;
                const auto        kept   = bundle->filament_presets.begin() + count;
                if (std::find(bundle->filament_presets.begin(), kept, edited) == kept &&
                    std::find(kept, bundle->filament_presets.end(), edited) != bundle->filament_presets.end())
                    return {{"error", "unsaved_filament_changes"},
                            {"detail", "the Filament tab has unsaved changes to '" + edited + "', used only by a slot being removed"},
                            {"hint", "save or discard them in the window, or select another filament preset with PUT /preset"}};
            }
        }

        while ((int) bundle->filament_presets.size() != count) {
            const size_t now = bundle->filament_presets.size();
            if ((int) now < count) sidebar.add_filament();
            else                   sidebar.delete_filament(); // the last slot, like the - button
            sidebar.update_filaments_counter();
            if (bundle->filament_presets.size() == now) // the sidebar refused: never loop forever
                return {{"error", "filament_change_failed"}};
        }
        if (count < before)
            clear_removed_slot_settings(plater->model(), count);
        // A new or removed slot changes every plate's G-code (the filament lists, the
        // flush matrix, objects moved to filament 1).
        plater->get_partplate_list().invalid_all_slice_result();
        api_notify("Filament slots: " + std::to_string(before) + " -> " + std::to_string(count));
        return filaments_json();
    }, 30);
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "bad_count")              return { 422, r };
        if (e == "filament_change_failed") return { 500, r };
        return { 409, r }; // preview_only, slot_in_use, unsaved_filament_changes
    }
    return { 200, r };
}

// PUT /filaments/{slot} {"preset"?: name, "color"?: "#rrggbb"}
// The preset goes through the steps of the sidebar's filament combo box
// (PresetComboBox::update_ams_color, then Plater::priv::on_select_preset), the colour
// through its colour picker (PlaterPresetComboBox::sync_colour_config, then
// Plater::priv::on_filament_color_changed).
Response Controller::handle_put_filament_slot(int slot, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_project
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (in.contains("preset") && !in["preset"].is_string())
        return { 400, {{"error", "preset_must_be_string"}} };
    if (in.contains("color") && !in["color"].is_string())
        return { 422, {{"error", "bad_color"}, {"detail", "\"#rrggbb\""}} };
    if (!in.contains("preset") && !in.contains("color"))
        return { 400, {{"error", "missing_fields"}, {"detail", "give \"preset\" and/or \"color\""}} };
    const bool        has_preset  = in.contains("preset");
    const std::string preset_name = in.value("preset", std::string());
    std::string       colour      = in.value("color", std::string());
    if (in.contains("color")) {
        const bool ok = colour.size() == 7 && colour[0] == '#' &&
                        std::all_of(colour.begin() + 1, colour.end(), [](unsigned char c) { return std::isxdigit(c) != 0; });
        if (!ok)
            return { 422, {{"error", "bad_color"}, {"detail", "\"#rrggbb\""}} };
        // Stored as the colour picker stores it (wxC2S_HTML_SYNTAX: "#RRGGBB").
        std::transform(colour.begin(), colour.end(), colour.begin(), [](unsigned char c) { return (char) std::toupper(c); });
    }

    nlohmann::json r = run_on_ui([slot, has_preset, preset_name, colour]() -> nlohmann::json {
        if (preview_only()) return {{"error", "preview_only"}};
        Plater       *plater  = wxGetApp().plater();
        PresetBundle *bundle  = wxGetApp().preset_bundle;
        Sidebar      &sidebar = wxGetApp().sidebar();
        const int     count   = (int) bundle->filament_presets.size();
        if (slot < 1 || slot > count || slot > (int) sidebar.combos_filament().size())
            return {{"error", "unknown_slot"}, {"count", count}};
        const size_t          idx   = size_t(slot - 1);
        PlaterPresetComboBox *combo = sidebar.combos_filament()[idx];

        // Check everything before changing anything.
        const Preset *preset         = nullptr;
        bool          preset_changed = false;
        if (has_preset) {
            preset = bundle->filaments.find_preset(preset_name, false);
            if (preset == nullptr || preset->is_default)
                return {{"error", "unknown_preset"}};
            // The combo box lists only presets compatible with the printer.
            if (!preset->is_compatible)
                return {{"error", "incompatible_preset"},
                        {"detail", "'" + preset_name + "' is not compatible with the selected printer preset"}};
            preset_changed = bundle->filament_presets[idx] != preset_name;
            // With a single slot the combo box also switches the Filament tab
            // (Tab::select_preset), which asks what to do with unsaved edits there.
            if (preset_changed && !sidebar.is_multifilament() && bundle->filaments.current_is_dirty())
                return {{"error", "unsaved_filament_changes"},
                        {"hint", "save or discard the Filament tab's changes in the window, or use PUT /preset"}};
        }

        // A newly picked preset brings its default colour, as update_ams_color does in
        // the combo box; an explicit "color" wins, and naming the slot's current preset
        // again keeps its colour. (Read now: the preset switch below may move presets.)
        std::string new_colour = colour;
        if (new_colour.empty() && preset_changed) {
            const auto *defaults = preset->config.option<ConfigOptionStrings>("default_filament_colour");
            if (defaults != nullptr && !defaults->values.empty())
                new_colour = defaults->values.front();
        }

        if (preset_changed) {
            // Plater::priv::on_select_preset, filament branch.
            bundle->set_filament_preset(idx, preset_name);
            plater->update_project_dirty_from_presets();
            bundle->export_selections(*wxGetApp().app_config);
            sidebar.update_dynamic_filament_list();
            if (!sidebar.is_multifilament())
                wxGetApp().get_tab(Preset::TYPE_FILAMENT)->select_preset(preset_name); // not dirty: checked above
            combo->update();
            plater->on_config_change(bundle->full_config());
        }

        bool colour_changed = false;
        if (!new_colour.empty() && to_lower(new_colour) != slot_colour(idx)) {
            combo->sync_colour_config({new_colour}, false);
            // Plater::priv::on_filament_color_changed: a plain colour replaces a
            // multi-colour filament synced from a printer.
            auto &multi_colour = bundle->ams_multi_color_filment;
            if (idx < multi_colour.size())
                multi_colour[idx].clear();
            colour_changed = true;
        }

        // The flush volumes follow the "Auto flush after changing..." preference, as in
        // the GUI: a colour change recalculates unless it is "disabled", a preset change
        // only when it is "all".
        const std::string auto_flush = wxGetApp().app_config->get("auto_calculate_flush");
        if ((colour_changed && auto_flush != "disabled") || (preset_changed && auto_flush == "all"))
            sidebar.auto_calc_flushing_volumes(int(idx));

        // The combo box marks every plate unsliced (Plater::priv::on_select_preset): the
        // slot's settings or colour are in every plate's G-code.
        if (preset_changed || colour_changed) {
            plater->get_partplate_list().invalid_all_slice_result();
            api_notify("Filament " + std::to_string(slot) + ": " + bundle->filament_presets[idx] + " " + slot_colour(idx));
        }
        const std::vector<int> plate_slots = plater->get_partplate_list().get_curr_plate()->get_extruders(true);
        return slot_json(idx, plate_slots);
    });
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_slot") return { 404, r };
        if (e == "unknown_preset" || e == "incompatible_preset") return { 422, r };
        return { 409, r }; // preview_only, unsaved_filament_changes
    }
    return { 200, r };
}

// --- POST /objects/{id}/split ------------------------------------------------------
//   {"to": "objects" | "parts", "volume_id"?: vid, "drop_to_bed"?: bool}
// Right-click > Split > To objects / To parts, without the dialogs those can open.

// How many objects ModelObject::split would make: one per solid part of an object
// with several volumes, one per separate shell of a single part, skipping pieces with
// fewer than 3 triangles as it does. Fewer than 2 means the window would show "The
// selected object couldn't be split." (Plater::priv::split_object).
static size_t split_object_count(const ModelObject &mo)
{
    size_t count = 0;
    if (mo.volumes.size() > 1) {
        for (const ModelVolume *mv : mo.volumes)
            if (mv->is_model_part() && mv->mesh().facets_count() >= 3)
                ++count;
        return count;
    }
    if (mo.volumes.empty() || !mo.volumes.front()->is_splittable())
        return mo.volumes.size();
    for (const TriangleMesh &piece : mo.volumes.front()->mesh().split())
        if (piece.facets_count() >= 3)
            ++count;
    return count;
}

Response Controller::handle_split_object(uint64_t id, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_project
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    const std::string to = in.contains("to") && in["to"].is_string() ? in["to"].get<std::string>() : "";
    if (to != "objects" && to != "parts")
        return { 422, {{"error", "bad_to"}, {"detail", "\"to\": \"objects\" | \"parts\""}} };
    if (in.contains("volume_id") && !in["volume_id"].is_number_unsigned())
        return { 400, {{"error", "bad_volume_id"}} };
    if (in.contains("drop_to_bed") && !in["drop_to_bed"].is_boolean())
        return { 400, {{"error", "drop_to_bed_must_be_boolean"}} };
    const bool     has_vid     = in.contains("volume_id");
    const uint64_t vid         = has_vid ? in["volume_id"].get<uint64_t>() : 0;
    const bool     drop_to_bed = in.value("drop_to_bed", true);

    // Splitting a big mesh takes a while (the shells are found twice: here, then by the
    // GUI's own split): allow 60 s.
    nlohmann::json r = run_on_ui([id, to, has_vid, vid, drop_to_bed]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        Model  &model  = plater->model();
        const int idx = find_object_index(model, id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = model.objects[idx];
        if (mo->instances.empty()) return {{"error", "no_instance"}};
        if (!plater->get_ui_job_worker().is_idle()) return {{"error", "job_running"}};
        if (editing_gizmo_open()) return {{"error", "gizmo_editing"}};
        const std::string name = mo->name;

        if (to == "objects") {
            if (split_object_count(*mo) < 2)
                return {{"error", "not_splittable"}, {"detail", "the object is a single solid piece"}};
            // The toolbar's split works on the 3D selection: select the object as a click
            // on it does, and let the object list follow.
            GLCanvas3D *canvas = plater->get_view3D_canvas3D();
            canvas->get_selection().add_object((unsigned int) idx, /*as_single_selection=*/true);
            wxGetApp().obj_list()->update_selections();
            if (plater->get_selected_object_idx() != idx) return {{"error", "select_failed"}};

            // When a piece would float above the bed, the split asks "Disable Auto-Drop
            // to preserve Z positioning?", but only for an object that auto-drops. So
            // switch that off on the original (it is replaced by the pieces anyway) and
            // split keeping every piece's height, which is the question's "Yes". Then, for
            // drop_to_bed, give the pieces the original's auto-drop back and put them on
            // the bed, which is what the "No" answer, or no question, ends with.
            const bool auto_drop = mo->instances.front()->auto_drop;
            mo->instances.front()->auto_drop = false;
            const size_t before = model.objects.size();
            plater->split_object(/*auto_drop=*/false);
            if (model.objects.size() <= before) { // nothing was split: never expected after the check above
                mo->instances.front()->auto_drop = auto_drop;
                return {{"error", "not_splittable"}};
            }
            // The original object was removed and the pieces appended at the end.
            std::vector<size_t> new_idxs;
            nlohmann::json      ids = nlohmann::json::array();
            for (size_t i = before - 1; i < model.objects.size(); ++i) {
                ModelObject *piece = model.objects[i];
                if (drop_to_bed) {
                    for (ModelInstance *inst : piece->instances)
                        inst->auto_drop = auto_drop;
                    piece->ensure_on_bed();
                }
                new_idxs.push_back(i);
                ids.push_back((uint64_t) piece->id().id);
            }
            if (drop_to_bed)
                plater->changed_objects(new_idxs); // reload the scene after the drops
            for (size_t i : new_idxs)
                invalidate_object_plates(int(i));
            api_notify("Split '" + name + "' into " + std::to_string(ids.size()) + " objects");
            return {{"ids", ids}};
        }

        // To parts: the object list splits the part it has selected (ObjectList::split).
        int vi = 0;
        if (has_vid) {
            vi = -1;
            for (size_t i = 0; i < mo->volumes.size(); ++i)
                if ((uint64_t) mo->volumes[i]->id().id == vid) vi = int(i);
            if (vi < 0) return {{"error", "unknown_volume"}};
        } else if (mo->volumes.size() > 1) {
            return {{"error", "not_splittable"},
                    {"detail", "the object has several parts: give \"volume_id\" (GET /objects/{id}/volumes)"}};
        }
        // ObjectList::split shows "The target object contains only one part and can not
        // be split." when the part is a single shell.
        if (!mo->volumes[vi]->is_splittable())
            return {{"error", "not_splittable"}, {"detail", "the part is a single solid piece"}};
        // An object with one part has no part rows in the list: its own row stands for it.
        ObjectList          *list  = wxGetApp().obj_list();
        ObjectDataViewModel *model_view = list->GetModel();
        const wxDataViewItem item = mo->volumes.size() > 1 ? model_view->GetItemByVolumeId(idx, vi) : model_view->GetItemById(idx);
        if (!item.IsOk()) return {{"error", "select_failed"}};
        list->select_item(item);
        list->update_selections_on_canvas(); // the 3D view follows, as after a click in the list
        if (list->get_selected_obj_idx() != idx || !list->is_splittable(false)) return {{"error", "select_failed"}};
        list->split();
        invalidate_object_plates(idx);
        nlohmann::json ids = nlohmann::json::array();
        for (const ModelVolume *mv : mo->volumes)
            ids.push_back((uint64_t) mv->id().id);
        api_notify("Split a part of '" + name + "': it has " + std::to_string(ids.size()) + " parts now");
        return {{"ids", ids}, {"object_id", id}};
    }, 60);
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_object" || e == "unknown_volume" || e == "no_instance") return { 404, r };
        if (e == "not_splittable") return { 422, r };
        return { 409, r }; // job_running, gizmo_editing, select_failed
    }
    return { 200, r };
}

// --- POST /undo, POST /redo ----------------------------------------------------------
// The window's Ctrl+Z / Ctrl+Y (Plater::undo / redo) on the main undo stack: object,
// part, plate and paint edits, the same history the window's undo list shows. Presets
// and settings are not in it (as in the window).
Response Controller::handle_undo_redo(bool undo)
{
    nlohmann::json r = run_on_ui([this, undo]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        // Model edits under a running slice, arrange/orient job or an open editing tool
        // would pull the data from under them.
        if (plater->is_background_process_slicing()) return {{"error", "slicing"}};
        if (!plater->get_ui_job_worker().is_idle()) return {{"error", "job_running"}};
        if (editing_gizmo_open()) return {{"error", "gizmo_editing"}};

        // With no editing tool open the window works on the main stack. The has_*
        // checks skip snapshots that change nothing, as Plater::undo / redo do.
        const UndoRedo::Stack &stack = plater->undo_redo_stack_main();
        bool ok = false;
        if (undo ? stack.has_undo_snapshot() : stack.has_redo_snapshot()) {
            std::string what; // the snapshot's name, for the notification
            plater->undo_redo_topmost_string_getter(undo, what);
            const size_t time_before = stack.active_snapshot_time();
            if (undo) plater->undo();
            else      plater->redo();
            ok = stack.active_snapshot_time() != time_before;
            if (ok) {
                // A snapshot may change objects on any plate, or the plates themselves.
                plater->get_partplate_list().invalid_all_slice_result();
                sync_slice_state_to_current_plate("slice.plate_changed");
                api_notify(std::string(undo ? "Undo" : "Redo") + (what.empty() ? "" : ": " + what));
            }
        }
        return {{"ok", ok}, {"can_undo", stack.has_undo_snapshot()}, {"can_redo", stack.has_redo_snapshot()}};
    });
    if (r.contains("error")) return { 409, r }; // slicing, job_running, gizmo_editing
    return { 200, r };
}

// --- /plates/{i}/layer_changes -----------------------------------------------------
// What the preview's layer slider adds at a layer (right-click a layer's "+"): a
// filament change, a pause or custom G-code. They live in
// Model::plates_custom_gcodes[i] as CustomGCode::Item, the slider's own storage, so a
// change made here shows on the slider and one made there shows here.

static const double k_same_z = 1e-3; // mm: two changes this close are at the same height

static const char *layer_change_type_name(CustomGCode::Type type)
{
    switch (type) {
    case CustomGCode::ToolChange:  return "tool_change";
    case CustomGCode::ColorChange: return "color_change";
    case CustomGCode::PausePrint:  return "pause";
    default:                       return "custom"; // Custom, Template (and Unknown)
    }
}

static nlohmann::json layer_change_json(const CustomGCode::Item &item)
{
    nlohmann::json j = {{"z", item.print_z}, {"type", layer_change_type_name(item.type)}};
    if (item.type == CustomGCode::ToolChange || item.type == CustomGCode::ColorChange)
        j["slot"] = item.extruder;
    if (item.type == CustomGCode::PausePrint && !item.extra.empty())
        j["message"] = item.extra;
    if (item.type == CustomGCode::Custom && !item.extra.empty())
        j["gcode"] = item.extra; // read-only here: set in the window
    return j;
}

static nlohmann::json layer_changes_json(const Model &model, int plate)
{
    nlohmann::json items = nlohmann::json::array();
    const auto it = model.plates_custom_gcodes.find(plate);
    if (it != model.plates_custom_gcodes.end())
        for (const CustomGCode::Item &item : it->second.gcodes)
            items.push_back(layer_change_json(item));
    return {{"plate", plate}, {"items", items}};
}

// Top of the highest object copy on the plate; 0 when the plate is empty.
static double plate_top_z(PartPlate &plate, const Model &model)
{
    double top = 0.;
    for (size_t oi = 0; oi < model.objects.size(); ++oi)
        for (size_t ii = 0; ii < model.objects[oi]->instances.size(); ++ii)
            if (plate.contain_instance((int) oi, (int) ii))
                top = std::max(top, model.objects[oi]->instance_bounding_box(ii).max.z());
    return top;
}

// What the window does after the slider's ticks change (the EVT_CUSTOMEVT_TICKSCHANGED
// handler in Plater.cpp): the plate's G-code is stale, the project is modified, the
// slice button wakes up, and a preview on screen reloads its slider. GUI thread only.
static void layer_changes_changed(int plate_idx)
{
    Plater    *plater = wxGetApp().plater();
    PartPlate *plate  = plater->get_partplate_list().get_plate(plate_idx);
    plate->update_slice_result_valid_state(false);
    plater->set_plater_dirty(true);
    wxGetApp().mainframe->update_slice_print_status(MainFrame::eEventSliceUpdate, true, false);
    if (plate_idx == plater->get_partplate_list().get_curr_plate_index() && plater->is_preview_shown())
        plater->reload_print();
}

Response Controller::handle_get_layer_changes(int plate)
{
    nlohmann::json r = run_on_ui([plate]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int count = plater->get_partplate_list().get_plate_count();
        if (plate >= count) return {{"error", "unknown_plate"}, {"count", count}};
        return layer_changes_json(plater->model(), plate);
    });
    if (r.contains("error")) return { 404, r };
    return { 200, r };
}

// POST /plates/{i}/layer_changes {"z": mm, "type": "tool_change" | "pause", "slot"?, "message"?}
Response Controller::handle_add_layer_change(int plate, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_project
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (!in.contains("z") || !in["z"].is_number() || !std::isfinite(in["z"].get<double>()))
        return { 422, {{"error", "bad_z"}, {"detail", "z: the height in mm where the change happens"}} };
    const double      z    = in["z"].get<double>();
    const std::string type = in.contains("type") && in["type"].is_string() ? in["type"].get<std::string>() : "";
    if (type == "color_change")
        // This slicer writes no G-code for a color change (GCode.cpp keeps only pauses and
        // custom G-code at a layer, and the slider no longer offers color changes); a
        // filament change is tool_change.
        return { 422, {{"error", "bad_type"},
                       {"detail", "color_change writes no G-code in ElegooSlicer; use tool_change (a filament slot) or pause"}} };
    if (type != "tool_change" && type != "pause")
        return { 422, {{"error", "bad_type"}, {"detail", "tool_change | pause (custom G-code is added in the window)"}} };
    if (in.contains("slot") && !in["slot"].is_number_integer())
        return { 422, {{"error", "bad_slot"}, {"detail", "slot: a 1-based filament slot"}} };
    if (in.contains("message") && !in["message"].is_string())
        return { 400, {{"error", "message_must_be_string"}} };
    if (type == "tool_change" && !in.contains("slot"))
        return { 422, {{"error", "bad_slot"}, {"detail", "tool_change needs \"slot\", the filament to change to"}} };
    const int         slot    = in.value("slot", 0);
    const std::string message = in.value("message", std::string());

    nlohmann::json r = run_on_ui([plate, z, type, slot, message]() -> nlohmann::json {
        Plater        *plater = wxGetApp().plater();
        PartPlateList &plates = plater->get_partplate_list();
        if (plate >= plates.get_plate_count()) return {{"error", "unknown_plate"}, {"count", plates.get_plate_count()}};
        if (preview_only()) return {{"error", "preview_only"}};
        if (plater->is_background_process_slicing()) return {{"error", "slicing"}};
        PartPlate *p   = plates.get_plate(plate);
        Model     &model = plater->model();

        const double top = plate_top_z(*p, model);
        if (top <= 0.)
            return {{"error", "bad_z"}, {"detail", "plate " + std::to_string(plate) + " has no objects"}};
        // At or above the top nothing prints after the change, and the slider drops a
        // change above the last layer (Preview::check_layers_slider_values).
        if (!(z > 0. && z < top))
            return {{"error", "bad_z"}, {"detail", "0 < z < " + std::to_string(top) + " (the plate's tallest object)"}};
        // The slider takes no changes when the plate prints object by object
        // (IMSlider::SetTicksValues drops them all in that mode).
        if (p->get_real_print_seq() == PrintSequence::ByObject)
            return {{"error", "bad_type"}, {"detail", "the plate prints object by object (print_sequence); layer changes need by layer"}};

        // The slider's mode, as Preview::update_layers_slider_mode works it out: with
        // several slots and a plate printed with one of them, a tool change switches the
        // whole plate to another slot from that height on.
        const int filaments = wxGetApp().filaments_cnt();
        int       only_slot = -1;
        bool      one_slot  = true;
        if (filaments > 1) {
            const std::vector<int> used = p->get_extruders_without_support();
            if (!used.empty()) only_slot = used.front();
            for (int s : used)
                if (s != only_slot) one_slot = false;
        }
        // A pause as the slider adds it (IMSlider::add_code_as_tick): no colour, the
        // plate's filament as extruder, the message kept with it.
        CustomGCode::Item item { z, CustomGCode::PausePrint, std::max(1, only_slot), "", message };
        if (type == "tool_change") {
            // The slider's "Change Filament" menu: only with 2+ slots, a plate printed
            // with a single slot (paint or parts in other colours already change
            // filament) and no spiral vase.
            if (filaments < 2)
                return {{"error", "bad_type"}, {"detail", "a tool change needs 2 or more filament slots"}};
            if (slot < 1 || slot > filaments)
                return {{"error", "bad_slot"}, {"detail", "the project has " + std::to_string(filaments) + " filament slot(s)"}};
            if (!one_slot)
                return {{"error", "bad_type"},
                        {"detail", "the plate already prints with several filaments (parts, paint or height ranges); "
                                   "a layer tool change needs a plate printed with one"}};
            if (wxGetApp().preset_bundle->full_config().opt_bool("spiral_mode"))
                return {{"error", "bad_type"}, {"detail", "no tool changes in spiral vase mode"}};
            const std::vector<std::string> colours = plater->get_extruder_colors_from_plater_config();
            item.type     = CustomGCode::ToolChange;
            item.extruder = slot;
            item.color    = size_t(slot - 1) < colours.size() ? colours[slot - 1] : std::string();
            item.extra    = "";
        }

        CustomGCode::Info &info = model.plates_custom_gcodes[plate];
        // Same height: the new change replaces the old one, as picking another entry on a
        // slider tick does, but never a custom G-code someone wrote in the window.
        for (auto it = info.gcodes.begin(); it != info.gcodes.end(); ++it) {
            if (std::abs(it->print_z - z) > k_same_z) continue;
            if (it->type == CustomGCode::Custom || it->type == CustomGCode::Template)
                return {{"error", "bad_z"}, {"detail", "a custom G-code is at this height; remove it first (DELETE)"}};
            info.gcodes.erase(it);
            break;
        }
        // Kept sorted by height, like the slider's list (ToolOrdering relies on it).
        const auto pos = std::upper_bound(info.gcodes.begin(), info.gcodes.end(), item);
        info.gcodes.insert(pos, item);
        // The mode the slider writes with its ticks (IMSlider::GetTicksValues): the G-code
        // generator applies tool changes only in MultiAsSingle mode.
        info.mode = only_slot < 0 ? CustomGCode::SingleExtruder : CustomGCode::MultiAsSingle;

        layer_changes_changed(plate);
        api_notify("Plate " + std::to_string(plate + 1) + ": " +
                   (type == "tool_change" ? "filament " + std::to_string(slot) : std::string("pause")) +
                   " at " + std::to_string(z) + " mm");
        return layer_changes_json(model, plate);
    });
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_plate") return { 404, r };
        if (e == "slicing" || e == "preview_only") return { 409, r };
        return { 422, r }; // bad_z, bad_slot, bad_type
    }
    return { 201, r };
}

// DELETE /plates/{i}/layer_changes?z=<mm>
Response Controller::handle_delete_layer_change(int plate, const std::string &target)
{
    // The z query value, e.g. "?z=12.4".
    double z = 0.;
    {
        const size_t q   = target.find('?');
        const size_t pos = q == std::string::npos ? std::string::npos : target.find("z=", q);
        if (pos == std::string::npos || (target[pos - 1] != '?' && target[pos - 1] != '&'))
            return { 400, {{"error", "missing_z"}, {"detail", "DELETE /plates/{i}/layer_changes?z=<mm>"}} };
        const std::string value = target.substr(pos + 2, target.find('&', pos) - pos - 2);
        try {
            size_t used = 0;
            z = std::stod(value, &used);
            if (used != value.size() || !std::isfinite(z)) throw std::invalid_argument("not a number");
        } catch (const std::exception &) {
            return { 400, {{"error", "bad_z"}, {"detail", "z must be a number in mm"}} };
        }
    }

    nlohmann::json r = run_on_ui([plate, z]() -> nlohmann::json {
        Plater        *plater = wxGetApp().plater();
        PartPlateList &plates = plater->get_partplate_list();
        if (plate >= plates.get_plate_count()) return {{"error", "unknown_plate"}, {"count", plates.get_plate_count()}};
        if (preview_only()) return {{"error", "preview_only"}};
        if (plater->is_background_process_slicing()) return {{"error", "slicing"}};
        Model &model = plater->model();
        auto   it    = model.plates_custom_gcodes.find(plate);
        if (it == model.plates_custom_gcodes.end()) return {{"error", "no_change_at_z"}};
        std::vector<CustomGCode::Item> &gcodes = it->second.gcodes;
        const auto found = std::find_if(gcodes.begin(), gcodes.end(),
                                        [z](const CustomGCode::Item &item) { return std::abs(item.print_z - z) <= k_same_z; });
        if (found == gcodes.end()) return {{"error", "no_change_at_z"}};
        const std::string what = layer_change_type_name(found->type);
        gcodes.erase(found);
        layer_changes_changed(plate);
        api_notify("Plate " + std::to_string(plate + 1) + ": removed the " + what + " at " + std::to_string(z) + " mm");
        return layer_changes_json(model, plate);
    });
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_plate" || e == "no_change_at_z") return { 404, r };
        return { 409, r }; // slicing, preview_only
    }
    return { 200, r };
}

// --- slice breakdown: purge ----------------------------------------------------------
// stats.breakdown.purge of GET /slice/status: what changing filament costs, with the
// numbers the preview's legend shows. The G-code processor counts the changes, adds
// their time to the load/unload/tool-change totals (on the Centauri Carbon 2 the time of
// each CANVAS change, M6211, is estimated in ElegooGCodeProcessorHelper.cpp) and keeps
// the purged volume per filament (the legend's "Flushed" column). Those times sit on
// tool-change moves, so no extrusion role of the breakdown includes them.
nlohmann::json breakdown_purge_json(const GCodeProcessorResult &res)
{
    const PrintEstimatedStatistics &stats = res.print_statistics;
    double flush_mm = 0., flush_g = 0.;
    for (const auto &[filament, volume_mm3] : stats.flush_per_filament) { // 0-based filament index
        // Same conversion as the legend (GCodeViewer, get_used_filament_from_volume).
        const double diameter = filament < res.filament_diameters.size() ? res.filament_diameters[filament] : 1.75;
        const double density  = filament < res.filament_densities.size() ? res.filament_densities[filament] : 0.;
        if (diameter > 0.)
            flush_mm += volume_mm3 / (PI * 0.25 * diameter * diameter);
        flush_g += volume_mm3 * density * 0.001;
    }
    return {
        {"filament_changes", stats.total_filament_changes},
        // The legend's tool-change time (GCodeViewer: load + unload + tool change).
        {"filament_change_time_s", stats.total_filament_load_time + stats.total_filament_unload_time + stats.total_tool_change_time},
        {"flush_g", flush_g},
        {"flush_mm", flush_mm},
    };
}

}}} // namespace
