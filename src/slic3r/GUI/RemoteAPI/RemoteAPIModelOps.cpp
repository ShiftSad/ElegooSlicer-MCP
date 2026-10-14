// src/slic3r/GUI/RemoteAPI/RemoteAPIModelOps.cpp
//
// elegoo-mcp: model operations ElegooSlicer MCP adds to the Remote API.
// Each handler runs its GUI work inside run_on_ui and repeats the steps of the
// matching GUI action, leaving out that action's dialogs: an API call must never
// open a modal window, because nobody may be at the machine to close it.
#include "RemoteAPIController.hpp"

#include "libslic3r/Format/bbs_3mf.hpp"   // save_object_mesh (auto-backup)
#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleMesh.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/GLCanvas3D.hpp"
#include "slic3r/GUI/GUI_ObjectList.hpp"
#include "slic3r/GUI/I18N.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/ObjectDataViewModel.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/Selection.hpp"

#include <algorithm>
#include <cmath>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// --- POST /orient {"object_id": id} ------------------------------------------
// The window auto-orients the 3D selection (OrientJob::prepare_selected), so this
// selects the object as a click on it does and starts the same orient job.
Response Controller::handle_orient_object(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (!in.contains("object_id"))
        return handle_orient(); // "{}" means every object, like no body
    if (!in["object_id"].is_number_unsigned())
        return { 400, {{"error", "bad_object_id"}} };
    const uint64_t id = in["object_id"].get<uint64_t>();

    nlohmann::json r = run_on_ui([id]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        if (!plater->get_ui_job_worker().is_idle()) return {{"error", "job_running"}};

        // Select only this object, the way Plater::priv::load_files selects new objects,
        // and let the object list follow the 3D scene.
        GLCanvas3D *canvas = plater->get_view3D_canvas3D();
        canvas->get_selection().add_object((unsigned int) idx, /*as_single_selection=*/true);
        wxGetApp().obj_list()->update_selections();
        if (canvas->get_gizmos_manager().is_enabled())
            canvas->update_gizmos_on_off_state();

        // Same reset as handle_orient: Elegoo's sticky "outside bed" prepare state makes
        // the job skip the selection.
        plater->set_prepare_state(Job::PREPARE_STATE_DEFAULT);
        plater->orient();
        return {{"started", true}, {"object_id", id}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object") return { 404, r };
        return { 409, r }; // job_running
    }
    return { 202, r };
}

// --- POST /view {"view": "prepare" | "preview"} ------------------------------
// Which main-window tab is showing: "prepare" (3D editor), "preview" or "other"
// (home, device, ... pages).
std::string main_view_name()
{
    const int tab = wxGetApp().mainframe->current_tab();
    if (tab == MainFrame::tp3DEditor) return "prepare";
    if (tab == MainFrame::tpPreview)  return "preview";
    return "other";
}

// Switches the main window to the Prepare or Preview tab with the call the GUI uses
// for this (MainFrame::select_tab).
Response Controller::handle_set_view(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object() || !in.contains("view") || !in["view"].is_string())
        return { 400, {{"error", "missing_view"}, {"detail", "body {\"view\": \"prepare\" | \"preview\"}"}} };
    const std::string view = in["view"].get<std::string>();
    if (view != "prepare" && view != "preview")
        return { 422, {{"error", "bad_view"}, {"detail", "prepare | preview"}} };

    nlohmann::json r = run_on_ui([view]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        // With a G-code file open the window is preview-only, and its Prepare button
        // asks whether to close that file (MainFrame::preview_only_hint). An API call
        // cannot answer a question, so refuse instead.
        if (view == "prepare" && (plater->only_gcode_mode() || plater->using_exported_file()))
            return {{"error", "preview_only"}};
        wxGetApp().mainframe->select_tab(size_t(view == "prepare" ? MainFrame::tp3DEditor : MainFrame::tpPreview));
        return {{"view", main_view_name()}};
    });
    if (r.contains("error")) return { 409, r };
    return { 200, r };
}

// --- part volumes: /objects/{id}/volumes -------------------------------------
// The parts, modifiers, negative volumes and support enforcers/blockers listed under
// an object in the object list. Ids are ModelVolume ids, like the object ids.

static const char *volume_type_name(ModelVolumeType type)
{
    switch (type) {
    case ModelVolumeType::MODEL_PART:         return "part";
    case ModelVolumeType::PARAMETER_MODIFIER: return "modifier";
    case ModelVolumeType::NEGATIVE_VOLUME:    return "negative";
    case ModelVolumeType::SUPPORT_ENFORCER:   return "support_enforcer";
    case ModelVolumeType::SUPPORT_BLOCKER:    return "support_blocker";
    default:                                  return "other";
    }
}

static int find_volume_index(const ModelObject &mo, uint64_t vid)
{
    for (size_t i = 0; i < mo.volumes.size(); ++i)
        if ((uint64_t) mo.volumes[i]->id().id == vid)
            return (int) i;
    return -1;
}

// One volume as JSON. Size and position are in world mm, measured on the object's
// first copy: the same frame POST /objects/{id}/volumes takes them in.
static nlohmann::json volume_json(const ModelObject &mo, size_t index)
{
    const ModelVolume *mv = mo.volumes[index];
    Transform3d world = mv->get_matrix();
    if (!mo.instances.empty())
        world = mo.instances.front()->get_matrix() * world;
    const BoundingBoxf3 bb     = mv->mesh().transformed_bounding_box(world);
    const Vec3d         size   = bb.size();
    const Vec3d         center = bb.center();

    const DynamicPrintConfig overrides = mv->config.get();
    nlohmann::json cfg = nlohmann::json::object();
    for (const std::string &key : overrides.keys())
        cfg[key] = overrides.opt_serialize(key);
    return {
        {"id", (uint64_t) mv->id().id},
        {"index", index},
        {"name", mv->name},
        {"type", volume_type_name(mv->type())},
        {"extruder", mv->config.has("extruder") ? mv->config.extruder() : 0}, // 0 = inherits
        {"size_mm", {size.x(), size.y(), size.z()}},
        {"position_mm", {center.x(), center.y(), center.z()}},
        {"config", cfg},
    };
}

// Reads a [x, y, z] array of finite numbers.
static bool read_vec3(const nlohmann::json &in, const char *key, Vec3d &out)
{
    if (!in.contains(key) || !in[key].is_array() || in[key].size() != 3)
        return false;
    for (int i = 0; i < 3; ++i) {
        if (!in[key][i].is_number()) return false;
        out[i] = in[key][i].get<double>();
        if (!std::isfinite(out[i])) return false;
    }
    return true;
}

Response Controller::route_volumes(const Request &req, uint64_t id, const std::string &action)
{
    if (action == "volumes") {
        if (req.method == "GET")  return handle_get_volumes(id);
        if (req.method == "POST") return handle_add_volume(id, req.body);
        return { 404, {{"error", "not_found"}} };
    }
    // "volumes/<vid>" or "volumes/<vid>/config"
    const std::string rest   = action.substr(std::string("volumes/").size());
    const size_t      slash  = rest.find('/');
    const std::string vid_s  = rest.substr(0, slash);
    const std::string sub    = slash == std::string::npos ? std::string() : rest.substr(slash + 1);
    uint64_t vid = 0;
    try {
        size_t used = 0;
        vid = std::stoull(vid_s, &used);
        if (used != vid_s.size()) throw std::invalid_argument("trailing characters");
    } catch (...) {
        return { 400, {{"error", "bad_volume_id"}} };
    }
    if (req.method == "DELETE" && sub.empty())    return handle_delete_volume(id, vid);
    if (req.method == "PUT" && sub == "config")   return handle_put_volume_config(id, vid, req.body);
    return { 404, {{"error", "not_found"}} };
}

Response Controller::handle_get_volumes(uint64_t id)
{
    nlohmann::json r = run_on_ui([id]() -> nlohmann::json {
        const Model &model = wxGetApp().plater()->model();
        const int idx = find_object_index(model, id);
        if (idx < 0) return {{"error", "unknown_object"}};
        const ModelObject *mo = model.objects[idx];
        nlohmann::json volumes = nlohmann::json::array();
        for (size_t i = 0; i < mo->volumes.size(); ++i)
            volumes.push_back(volume_json(*mo, i));
        return {{"object", id}, {"volumes", volumes}};
    });
    if (r.contains("error")) return { 404, r };
    return { 200, r };
}

// POST /objects/{id}/volumes
//   {"type": "modifier|negative|support_enforcer|support_blocker",
//    "shape": "box|cylinder|sphere", "size_mm": [x,y,z], "position_mm": [x,y,z],
//    "name"?: "...", "config"?: {...}}
// Repeats ObjectList::load_generic_subobject (right-click > Add modifier > Cube...)
// without its "Add Modifier" tips dialog, with the size and position asked for
// instead of the menu's fixed size beside the object.
Response Controller::handle_add_volume(uint64_t id, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };

    const std::string type_s = in.contains("type") && in["type"].is_string() ? in["type"].get<std::string>() : "";
    ModelVolumeType type = type_s == "modifier"         ? ModelVolumeType::PARAMETER_MODIFIER :
                           type_s == "negative"         ? ModelVolumeType::NEGATIVE_VOLUME :
                           type_s == "support_enforcer" ? ModelVolumeType::SUPPORT_ENFORCER :
                           type_s == "support_blocker"  ? ModelVolumeType::SUPPORT_BLOCKER :
                                                          ModelVolumeType::INVALID;
    if (type == ModelVolumeType::INVALID)
        return { 422, {{"error", "bad_type"}, {"detail", "modifier | negative | support_enforcer | support_blocker"}} };

    const std::string shape = in.contains("shape") && in["shape"].is_string() ? in["shape"].get<std::string>() : "";
    if (shape != "box" && shape != "cylinder" && shape != "sphere")
        return { 422, {{"error", "bad_shape"}, {"detail", "box | cylinder | sphere"}} };

    Vec3d size, position;
    if (!read_vec3(in, "size_mm", size) || size.minCoeff() <= 0. || size.maxCoeff() > 10000.)
        return { 422, {{"error", "bad_size"}, {"detail", "size_mm: [x, y, z], each > 0 and <= 10000 mm"}} };
    if (!read_vec3(in, "position_mm", position))
        return { 422, {{"error", "bad_position"}, {"detail", "position_mm: [x, y, z], world mm of the centre"}} };
    if (in.contains("name") && !in["name"].is_string())
        return { 400, {{"error", "name_must_be_string"}} };
    if (in.contains("config") && !in["config"].is_object())
        return { 400, {{"error", "config_must_be_object"}} };
    const std::string    name      = in.value("name", std::string());
    const nlohmann::json config_in = in.value("config", nlohmann::json::object());

    nlohmann::json r = run_on_ui([id, type, type_s, shape, size, position, name, config_in]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        if (mo->instances.empty()) return {{"error", "no_instance"}};

        // Check the overrides before adding anything, so a refused key adds nothing.
        // A new modifier starts on "default" filament, as in the GUI.
        DynamicPrintConfig overrides;
        overrides.set_key_value("extruder", new ConfigOptionInt(0));
        nlohmann::json applied = nlohmann::json::array();
        nlohmann::json errors  = nlohmann::json::object();
        stage_object_config(overrides, config_in, applied, errors);
        if (!errors.empty()) {
            api_notify("Couldn't add a " + type_s + " to '" + mo->name + "'", true);
            return {{"error", "config_rejected"}, {"applied", nlohmann::json::array()}, {"errors", errors}};
        }

        plater->take_snapshot("Add primitive");

        // The menu's primitives (create_mesh in GUI_ObjectList.cpp), made at unit size
        // and scaled, so a cylinder or sphere may be elliptic.
        TriangleMesh mesh;
        wxString     shape_label;
        if (shape == "box") {
            mesh        = TriangleMesh(its_make_cube(1., 1., 1.));
            shape_label = _L("Cube");
        } else if (shape == "cylinder") {
            mesh        = TriangleMesh(its_make_cylinder(0.5, 1.));
            shape_label = _L("Cylinder");
        } else {
            mesh        = TriangleMesh(its_make_sphere(0.5, PI / 90));
            shape_label = _L("Sphere");
        }
        mesh.scale(size.cast<float>());

        // add_volume centres the mesh on the volume origin. Then, as the GUI does,
        // undo the copy's rotation and scale so the shape stays aligned with the bed,
        // and put its centre on the requested world position.
        ModelVolume *mv = mo->add_volume(std::move(mesh), type);
        const Geometry::Transformation &inst = mo->instances.front()->get_transformation();
        const Transform3d inst_inverse = inst.get_matrix_no_offset().inverse();
        mv->set_transformation(inst_inverse);
        mv->set_offset(inst_inverse * (position - inst.get_offset()));

        Slic3r::save_object_mesh(*mo); // the auto-backup copy of the object, as the GUI does
        mv->name = name.empty() ? into_u8(_L("Generic") + "-" + shape_label) : name;
        mv->config.assign_config(std::move(overrides));
        mv->source.is_from_builtin_objects = true;

        // Rebuild the object's rows in the object list (it sorts the volumes by type),
        // reload the 3D scene and reschedule slicing, as the GUI's add does.
        wxGetApp().obj_list()->reorder_volumes_and_get_selection(idx);
        invalidate_object_plates(idx);
        api_notify("Added " + mv->name + " to '" + mo->name + "'");

        const auto it = std::find(mo->volumes.begin(), mo->volumes.end(), mv);
        return {{"volume", volume_json(*mo, size_t(it - mo->volumes.begin()))}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object" || r["error"] == "no_instance") return { 404, r };
        return { 422, r }; // config_rejected
    }
    return { 201, r };
}

// PUT /objects/{id}/volumes/{vid}/config - same atomic semantics and key policy as
// PUT /objects/{id}/config, applied to one volume's own overrides.
Response Controller::handle_put_volume_config(uint64_t id, uint64_t vid, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    nlohmann::json r = run_on_ui([id, vid, in]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        const int vi = find_volume_index(*mo, vid);
        if (vi < 0) return {{"error", "unknown_volume"}};
        ModelVolume *mv = mo->volumes[vi];

        DynamicPrintConfig cfg = mv->config.get(); // copy of the volume's overrides
        nlohmann::json applied = nlohmann::json::array();
        nlohmann::json errors  = nlohmann::json::object();
        stage_object_config(cfg, in, applied, errors);
        if (!errors.empty()) { // atomic: apply nothing on any error
            api_notify("Couldn't set settings on '" + mv->name + "'", true);
            return {{"applied", nlohmann::json::array()}, {"errors", errors}};
        }
        mv->config.assign_config(std::move(cfg));
        plater->changed_object(idx);
        invalidate_object_plates(idx);

        // The object list draws the volume's filament and its settings row itself
        // (see handle_put_object_config), so refresh both.
        ObjectList          *obj_list = wxGetApp().obj_list();
        ObjectDataViewModel *om       = obj_list->GetModel();
        wxDataViewItem       item     = om->GetItemByVolumeId(idx, vi);
        if (item.IsOk()) {
            if (in.contains("extruder")) {
                const int ext = mv->config.has("extruder") ? mv->config.extruder() : 0;
                om->SetExtruder(ext == 0 ? _L("default") : wxString::Format("%d", ext), item);
            }
            obj_list->add_settings_item(item, &mv->config.get());
            obj_list->Refresh();
        }
        if (!applied.empty())
            api_notify("Updated " + std::to_string(applied.size()) + " setting(s) on '" + mv->name + "'");
        return {{"applied", applied}, {"errors", errors}, {"object", mo->name}, {"volume", volume_json(*mo, vi)}};
    });
    if (r.contains("error")) return { 404, r };
    return { r["errors"].empty() ? 200 : 422, r };
}

// DELETE /objects/{id}/volumes/{vid} - the object list's own delete
// (ObjectList::del_subobject_item), after refusing the cases where it would open a
// dialog: the last solid part (an error box) and a part of a cut object (a question).
Response Controller::handle_delete_volume(uint64_t id, uint64_t vid)
{
    nlohmann::json r = run_on_ui([id, vid]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        const int vi = find_volume_index(*mo, vid);
        if (vi < 0) return {{"error", "unknown_volume"}};
        const ModelVolume *mv = mo->volumes[vi];

        int parts = 0;
        for (const ModelVolume *v : mo->volumes)
            if (v->is_model_part()) ++parts;
        if (mv->is_model_part() && parts == 1)
            return {{"error", "last_part"}};
        if (mo->is_cut() && (mv->is_model_part() || mv->is_negative_volume()))
            return {{"error", "cut_object"}, {"detail", "parts of a cut object are removed in the window"}};

        // With a single volume the list shows no volume rows, but that case is the
        // last part, refused above.
        wxDataViewItem item = wxGetApp().obj_list()->GetModel()->GetItemByVolumeId(idx, vi);
        if (!item.IsOk()) return {{"error", "list_item_missing"}};
        const std::string vname = mv->name;
        wxGetApp().obj_list()->del_subobject_item(item);
        invalidate_object_plates(idx);
        api_notify("Removed '" + vname + "' from '" + mo->name + "'");
        return {{"ok", true}};
    });
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_object" || e == "unknown_volume") return { 404, r };
        if (e == "list_item_missing") return { 500, r };
        return { 409, r }; // last_part, cut_object
    }
    return { 200, r };
}

}}} // namespace
